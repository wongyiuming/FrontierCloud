package httpapi

import (
	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
)

// Even a Standalone node answers signed challenges so its HTTPS endpoint can be
// verified before promotion. Loopback HTTP is never a control-plane exception.
func RegisterNodeIdentity(router *gin.Engine, settings config.Config, resolver *network.Resolver, service *node.Service) {
	router.GET("/internal/v1/identity", func(c *gin.Context) {
		c.Header("Cache-Control", "no-store")
		identity := resolver.Resolve(c.Request)
		secure := c.Request.TLS != nil || (identity.FromTrustedProxy && len(c.Request.Header.Values("X-Forwarded-Proto")) == 1 && c.GetHeader("X-Forwarded-Proto") == "https")
		if !settings.TLSEnabled || !secure {
			detail(c, 403, "节点控制面仅允许已启用 TLS 的 HTTPS")
			return
		}
		challenge := c.Query("challenge")
		if len(c.Request.URL.Query()["challenge"]) != 1 || !node.ValidIdentifier(challenge) {
			detail(c, 400, "Invalid identity challenge")
			return
		}
		value, err := service.SignedIdentity(c.Request.Context(), challenge)
		if err != nil {
			internalError(c, err)
			return
		}
		c.JSON(200, value)
	})
}
