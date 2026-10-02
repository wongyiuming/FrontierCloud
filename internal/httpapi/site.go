package httpapi

import (
	"context"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/sitecontrol"
)

func RegisterSiteAdmin(router *gin.Engine, a *Admin, service *sitecontrol.Service) {
	group := router.Group("/api/v1/media/admin/site", a.transport, a.authenticate)
	group.GET("/maintenance", func(c *gin.Context) {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
		defer cancel()
		value, err := service.Status(ctx)
		if err != nil {
			internalError(c, err)
			return
		}
		c.JSON(200, value)
	})
	group.POST("/maintenance", func(c *gin.Context) {
		if !nodeHTTPS(c, a.settings, a.network) {
			return
		}
		var body struct {
			Enabled *bool `json:"enabled"`
		}
		if !decodeNodeAdmin(c, &body) {
			return
		}
		if body.Enabled == nil {
			invalid(c, "body", "enabled")
			return
		}
		ctx, cancel := context.WithTimeout(c.Request.Context(), 8*time.Second)
		defer cancel()
		source := "enabled=" + strconv.FormatBool(*body.Enabled)
		if err := a.auth.Audit(ctx, session(c).Hash, "maintenance_change", source, "pending", "", 1, a.info(c)); err != nil {
			internalError(c, err)
			return
		}
		value, err := service.Set(ctx, *body.Enabled)
		result, message := "success", ""
		if err != nil {
			result, message = "failed", "maintenance change rejected"
		}
		cleanup, stop := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Second)
		defer stop()
		if auditErr := a.auth.Audit(cleanup, session(c).Hash, "maintenance_change", source, result, message, 1, a.info(c)); auditErr != nil {
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
