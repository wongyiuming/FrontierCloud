package httpapi

import (
	"context"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/diagnostics"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
)

const diagnosticRelayPath = "/internal/v1/playback-continuity-diagnostics"

func diagnosticBody(c *gin.Context) ([]byte, bool) {
	controller := http.NewResponseController(c.Writer)
	_ = controller.SetReadDeadline(time.Now().Add(10 * time.Second))
	defer controller.SetReadDeadline(time.Time{})
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, diagnostics.MaxBodyBytes)
	b, err := io.ReadAll(c.Request.Body)
	if err != nil {
		var limit *http.MaxBytesError
		if errors.As(err, &limit) {
			detail(c, 413, "Diagnostic report too large")
		} else {
			detail(c, 400, "Invalid diagnostic report")
		}
		return nil, false
	}
	return b, true
}
func diagnosticPayload(c *gin.Context, b []byte) (map[string]any, bool) {
	p, err := diagnostics.Normalize(b)
	if err != nil {
		detail(c, 400, "Invalid diagnostic report")
		return nil, false
	}
	if p["diagnostic_id"] == "" || p["stage"] == "" {
		detail(c, 400, "Diagnostic identity and stage are required")
		return nil, false
	}
	return p, true
}
func RegisterPlaybackDiagnostics(router *gin.Engine, settings config.Config, resolver *network.Resolver, service *node.Service, a *Admin, reports *diagnostics.Store) {
	registerPlaybackDiagnostics(router, settings, resolver, service, a, reports, func() int64 { return time.Now().Unix() })
}
func registerPlaybackDiagnostics(router *gin.Engine, settings config.Config, resolver *network.Resolver, service *node.Service, a *Admin, reports *diagnostics.Store, now func() int64) {
	live := func(c *gin.Context) bool {
		c.Header("Cache-Control", "no-store")
		if !diagnostics.Enabled(now()) {
			detail(c, 410, "Temporary playback diagnostics retired")
			return false
		}
		return true
	}
	router.POST("/api/v1/media/playback-continuity-diagnostics", func(c *gin.Context) {
		if !live(c) {
			return
		}
		b, ok := diagnosticBody(c)
		if !ok {
			return
		}
		p, ok := diagnosticPayload(c, b)
		if !ok {
			return
		}
		identity, err := service.IdentityState(c.Request.Context())
		if err != nil {
			internalError(c, err)
			return
		}
		delivery := "standalone-local"
		if identity.Role == "Master" {
			delivery = "master-local"
		}
		if identity.Role == "Follower" {
			upstream, err := service.DiagnosticUpstream(c.Request.Context())
			if err != nil {
				internalError(c, err)
				return
			}
			if upstream != nil {
				ctx, cancel := context.WithTimeout(c.Request.Context(), 2500*time.Millisecond)
				_, err = service.Call(ctx, *upstream, diagnosticRelayPath, p)
				cancel()
				if err == nil {
					c.Status(202)
					return
				}
				delivery = "follower-fallback"
			}
		}
		if err := reports.Add(p, identity.ID, delivery, now()); err != nil {
			internalError(c, err)
			return
		}
		c.Status(202)
	})
	router.POST(diagnosticRelayPath, func(c *gin.Context) {
		if !live(c) || !nodeHTTPS(c, settings, resolver) {
			return
		}
		b, ok := diagnosticBody(c)
		if !ok {
			return
		}
		path := c.Request.URL.EscapedPath()
		if c.Request.URL.RawQuery != "" {
			path += "?" + c.Request.URL.RawQuery
		}
		relation, err := service.Authenticate(c.Request.Context(), c.Request.Header, c.Request.Method, path, b, false, false)
		if err != nil {
			if errors.Is(err, node.ErrAuthentication) {
				detail(c, 401, "Invalid relationship authentication")
			} else {
				internalError(c, err)
			}
			return
		}
		identity, err := service.IdentityState(c.Request.Context())
		if err != nil {
			internalError(c, err)
			return
		}
		if identity.Role != "Master" || relation.Direction != "downstream" {
			detail(c, 403, "Only a Master accepts playback diagnostic relay")
			return
		}
		p, ok := diagnosticPayload(c, b)
		if !ok {
			return
		}
		if err := reports.Add(p, relation.PeerID, "follower-relay", now()); err != nil {
			internalError(c, err)
			return
		}
		c.JSON(200, gin.H{"status": "accepted"})
	})
	group := router.Group("/api/v1/media/admin/playback-continuity-diagnostics", a.transport, a.authenticate)
	group.GET("", func(c *gin.Context) { c.JSON(200, reports.Snapshot(now())) })
	group.DELETE("", func(c *gin.Context) { reports.Clear(); c.JSON(200, gin.H{"status": "cleared"}) })
}
