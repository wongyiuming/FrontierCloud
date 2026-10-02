package httpapi

import (
	"context"
	"errors"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/release"
)

func RegisterReleaseAdmin(router *gin.Engine, a *Admin, service *release.Coordinator) {
	group := router.Group("/api/v1/media/admin/nodes/release", a.transport, a.authenticate)
	group.GET("", func(c *gin.Context) {
		refresh := c.Query("refresh_ci") == "true" || c.Query("refresh_ci") == "1"
		ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
		defer cancel()
		value, err := service.Status(ctx, refresh)
		if err != nil {
			internalError(c, err)
			return
		}
		c.JSON(200, value)
	})
	for _, mode := range []string{"upgrade", "rollback"} {
		group.POST("/"+mode, func(c *gin.Context) {
			if !nodeHTTPS(c, a.settings, a.network) {
				return
			}
			body, ok := nodeBody(c)
			if !ok {
				return
			}
			if len(body) != 0 {
				var value map[string]any
				if controlJSON(body, &value) != nil || value == nil || len(value) != 0 {
					detail(c, 400, "Release target comes from verified evidence, not the request body")
					return
				}
			}
			ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
			defer cancel()
			action := "release_" + mode
			source := "release_branch=" + service.Policy.Branch
			if err := a.auth.Audit(ctx, session(c).Hash, action, source, "pending", "", 1, a.info(c)); err != nil {
				internalError(c, err)
				return
			}
			value, err := service.Start(ctx, mode)
			result, message := "success", ""
			if err != nil {
				result, message = "failed", "release request rejected"
			}
			cleanup, stop := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Second)
			defer stop()
			if auditErr := a.auth.Audit(cleanup, session(c).Hash, action, source, result, message, 1, a.info(c)); auditErr != nil {
				internalError(c, auditErr)
				return
			}
			if err != nil {
				detail(c, 409, err.Error())
				return
			}
			c.JSON(200, value)
		})
	}
}

// Only a freshly authenticated active upstream controls a Follower's updater.
// Control framing, TLS and replay boundaries match every other node endpoint.
func RegisterNodeRelease(router *gin.Engine, settings config.Config, resolver *network.Resolver, service *node.Service, agent release.Agent) {
	for _, action := range []string{"start", "status"} {
		router.POST("/internal/v1/cluster-update/"+action, func(c *gin.Context) {
			if !nodeHTTPS(c, settings, resolver) {
				return
			}
			body, ok := nodeBody(c)
			if !ok {
				return
			}
			ctx, cancel := context.WithTimeout(c.Request.Context(), 10*time.Second)
			defer cancel()
			path := c.Request.URL.EscapedPath()
			if c.Request.URL.RawQuery != "" {
				path += "?" + c.Request.URL.RawQuery
			}
			rel, err := service.Authenticate(ctx, c.Request.Header, c.Request.Method, path, body, false, false)
			if err != nil {
				if errors.Is(err, node.ErrAuthentication) {
					detail(c, 401, "Invalid relationship authentication")
				} else {
					internalError(c, err)
				}
				return
			}
			if err = service.RequireFollower(ctx, rel); err != nil {
				detail(c, 403, "Only the paired Master can control Follower releases")
				return
			}
			var value struct {
				Target string `json:"target_sha"`
				Mode   string `json:"mode"`
			}
			if controlJSON(body, &value) != nil {
				detail(c, 400, "Invalid release request")
				return
			}
			if action == "status" {
				c.JSON(200, gin.H{"status": release.AgentStatus(ctx, agent)})
				return
			}
			if value.Mode == "" {
				value.Mode = "upgrade"
			}
			if !release.ValidSHA(value.Target) || value.Mode != "upgrade" && value.Mode != "rollback" {
				detail(c, 400, "Invalid release target")
				return
			}
			if agent == nil {
				detail(c, 409, "Follower updater unavailable")
				return
			}
			out, err := agent.Request(ctx, map[string]any{"action": "start", "target_sha": value.Target, "mode": value.Mode, "hold_maintenance": false})
			if err != nil {
				detail(c, 409, "Follower updater unavailable")
				return
			}
			if out["ok"] != true {
				status, _ := out["status"].(map[string]any)
				if status["target_sha"] == value.Target && (status["mode"] == nil || status["mode"] == value.Mode) && (release.Busy(status["state"]) || status["state"] == "success") {
					c.JSON(200, gin.H{"accepted": true, "status": status})
					return
				}
				detail(c, 409, "Follower updater rejected release")
				return
			}
			c.JSON(200, gin.H{"accepted": true, "target_sha": value.Target, "mode": value.Mode})
		})
	}
}
