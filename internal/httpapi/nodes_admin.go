package httpapi

import (
	"errors"
	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"unicode/utf8"
)

// The established Admin session/CSRF contract applies to every node operation;
// mutations additionally require the TLS-enabled node control-plane boundary.
func RegisterAdminNodes(router *gin.Engine, a *Admin, service *node.Service) {
	group := router.Group("/api/v1/media/admin/nodes", a.transport, a.authenticate)
	audit := func(c *gin.Context) store.NodeAudit {
		info := a.info(c)
		return store.NodeAudit{Actor: session(c).Hash, RequestID: info.RequestID, TraceID: info.TraceID}
	}
	mutate := func(c *gin.Context) {
		if !nodeHTTPS(c, a.settings, a.network) {
			c.Abort()
		}
	}
	group.GET("", func(c *gin.Context) {
		result, err := service.Status(c.Request.Context())
		if err != nil {
			internalError(c, err)
			return
		}
		result["storage_pool"] = nil
		if result["role"] == "Master" {
			pool, err := a.public.media.StoragePool(c.Request.Context())
			if err != nil {
				internalError(c, err)
				return
			}
			result["storage_pool"] = pool
		}
		c.JSON(200, result)
	})
	writes := group.Group("", mutate)
	writes.POST("/promote", func(c *gin.Context) {
		var body struct {
			Role     string `json:"role"`
			Endpoint string `json:"endpoint"`
			Capacity *int64 `json:"local_capacity_gib"`
		}
		if !decodeNodeAdmin(c, &body) {
			return
		}
		if (body.Role != "Master" && body.Role != "Follower") || body.Endpoint == "" || utf8.RuneCountInString(body.Endpoint) > 512 || body.Capacity != nil && (*body.Capacity < 1 || *body.Capacity > 10240) {
			invalid(c, "body", "promotion")
			return
		}
		allocation := int64(0)
		if body.Capacity != nil {
			allocation = *body.Capacity * store.GiB
		}
		row, err := service.Promote(c.Request.Context(), body.Role, body.Endpoint, allocation, audit(c))
		if err != nil {
			nodeAdminError(c, err)
			return
		}
		service.Wake()
		c.JSON(200, gin.H{"role": row.Role})
	})
	writes.POST("/reinitialize", func(c *gin.Context) {
		var body struct {
			Confirmation string `json:"confirmation"`
		}
		if !decodeNodeAdmin(c, &body) {
			return
		}
		if body.Confirmation == "" || utf8.RuneCountInString(body.Confirmation) > 32 {
			invalid(c, "body", "confirmation")
			return
		}
		row, err := service.Reinitialize(c.Request.Context(), body.Confirmation, audit(c))
		if err != nil {
			nodeAdminError(c, err)
			return
		}
		c.JSON(200, gin.H{"role": row.Role, "node_id": row.ID})
	})
	writes.POST("/pair-package", func(c *gin.Context) {
		value, err := service.CreatePair(c.Request.Context(), audit(c))
		if err != nil {
			nodeAdminError(c, err)
			return
		}
		c.JSON(200, value)
	})
	writes.POST("/pair", func(c *gin.Context) {
		var body struct {
			Package node.Envelope `json:"package"`
		}
		if !decodeNodeAdmin(c, &body) {
			return
		}
		id, err := service.ImportPair(c.Request.Context(), body.Package, audit(c))
		service.Wake() // A lost confirmation reply leaves a recoverable pending pair.
		if err != nil {
			nodeAdminError(c, err)
			return
		}
		c.JSON(200, gin.H{"relationship_id": id})
	})
	writes.POST("/:identifier/mode", func(c *gin.Context) {
		var body struct {
			Mode string `json:"mode"`
		}
		if !decodeNodeAdmin(c, &body) {
			return
		}
		if body.Mode != "Direct" && body.Mode != "Relay" {
			invalid(c, "body", "mode")
			return
		}
		if err := service.SetMode(c.Request.Context(), c.Param("identifier"), body.Mode, audit(c)); err != nil {
			nodeAdminError(c, err)
			return
		}
		c.JSON(200, gin.H{"mode": body.Mode})
	})
	writes.POST("/:identifier/resources", func(c *gin.Context) {
		var body struct {
			Storage  bool   `json:"storage_enabled"`
			Capacity *int64 `json:"storage_capacity_gib"`
			Backup   bool   `json:"backup_enabled"`
		}
		if !decodeNodeAdmin(c, &body) {
			return
		}
		if body.Capacity == nil || *body.Capacity < 0 || *body.Capacity > 10240 {
			invalid(c, "body", "storage_capacity_gib")
			return
		}
		var config store.ResourceConfiguration
		config.Storage.Enabled, config.Backup.Enabled = body.Storage, body.Backup
		if body.Storage {
			config.Storage.Allocation = *body.Capacity * store.GiB
		}
		if err := service.Configure(c.Request.Context(), c.Param("identifier"), config, audit(c)); err != nil {
			nodeAdminError(c, err)
			return
		}
		c.JSON(200, gin.H{"status": "ok"})
	})
	writes.POST("/:identifier/revoke", func(c *gin.Context) {
		if err := service.Revoke(c.Request.Context(), c.Param("identifier"), audit(c)); err != nil {
			nodeAdminError(c, err)
			return
		}
		service.Wake()
		c.JSON(200, gin.H{"state": "revoked"})
	})
}

func decodeNodeAdmin(c *gin.Context, target any) bool {
	body, ok := nodeBody(c)
	if !ok {
		return false
	}
	if err := controlJSON(body, target); err != nil {
		invalid(c, "body", "payload")
		return false
	}
	return true
}
func nodeAdminError(c *gin.Context, err error) {
	message := "节点 HTTPS 验证或通信失败；请检查证书、网络及节点身份"
	if errors.Is(err, store.ErrNodeState) || errors.Is(err, store.ErrStorageCapacity) {
		message = err.Error()
	}
	detail(c, 409, message)
}
