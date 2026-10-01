package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func storageCORS(c *gin.Context, relation store.Relationship) bool {
	c.Header("Vary", "Origin")
	origins := c.Request.Header.Values("Origin")
	if len(origins) > 1 || len(origins) == 1 && origins[0] != relation.Endpoint {
		detail(c, 403, "Unpaired storage origin")
		return false
	}
	if len(origins) == 1 {
		c.Header("Access-Control-Allow-Origin", origins[0])
		c.Header("Access-Control-Allow-Methods", "PUT, OPTIONS")
		c.Header("Access-Control-Allow-Headers", "Content-Type, X-Storage-Capability")
	}
	return true
}
func RegisterNodeStorage(router *gin.Engine, settings config.Config, resolver *network.Resolver, control *node.Service, volume *media.Service) {
	router.POST("/internal/v1/storage/:object/delete", func(c *gin.Context) {
		if !nodeHTTPS(c, settings, resolver) {
			return
		}
		token, ok := capabilityInput(c, "X-Storage-Capability")
		if !ok {
			detail(c, 401, "Storage capability invalid or expired")
			return
		}
		relation, value, err := control.VerifyOwnedStorage(c.Request.Context(), token, c.Param("object"), "delete")
		if err != nil {
			if errors.Is(err, node.ErrCapability) {
				detail(c, 401, "Storage capability invalid or expired")
			} else {
				internalError(c, err)
			}
			return
		}
		number, ok := value["size"].(json.Number)
		size, sizeErr := number.Int64()
		name, _ := value["path"].(string)
		if _, err := media.ValidateStorageObject(c.Param("object"), name, max(1, size)); err != nil || !ok || sizeErr != nil || size < 0 {
			detail(c, 401, "Storage capability invalid or expired")
			return
		}
		if !storageCORS(c, relation) {
			return
		}
		err = volume.OwnedDelete(c.Request.Context(), relation.ID, c.Param("object"), name, size, store.NodeAudit{Actor: relation.PeerID, RequestID: c.GetString("request_id"), TraceID: c.GetString("trace_id")})
		if err != nil {
			if errors.Is(err, store.ErrNodeState) {
				detail(c, 409, "Storage deletion conflict")
			} else {
				mediaAdminError(c, err)
			}
			return
		}
		c.JSON(200, gin.H{"status": "deleted"})
	})
	router.OPTIONS("/internal/v1/storage/:object", func(c *gin.Context) {
		if !nodeHTTPS(c, settings, resolver) {
			return
		}
		origins := c.Request.Header.Values("Origin")
		if len(origins) != 1 {
			detail(c, 403, "Unpaired storage origin")
			return
		}
		relation, err := control.FollowerOrigin(c.Request.Context(), origins[0])
		if err != nil {
			if errors.Is(err, node.ErrCapability) {
				detail(c, 403, "Unpaired storage origin")
			} else {
				internalError(c, err)
			}
			return
		}
		if !storageCORS(c, relation) {
			return
		}
		c.Status(200)
	})
	router.PUT("/internal/v1/storage/:object", func(c *gin.Context) {
		if !nodeHTTPS(c, settings, resolver) {
			return
		}
		token, ok := capabilityInput(c, "X-Storage-Capability")
		if !ok {
			detail(c, 401, "Storage capability invalid or expired")
			return
		}
		relation, value, err := control.VerifyOwnedStorage(c.Request.Context(), token, c.Param("object"), "upload")
		if err != nil {
			if errors.Is(err, node.ErrCapability) {
				detail(c, 401, "Storage capability invalid or expired")
			} else {
				internalError(c, err)
			}
			return
		}
		sizeValue, ok := value["size"].(json.Number)
		size, sizeErr := sizeValue.Int64()
		name, _ := value["path"].(string)
		object, err := media.ValidateStorageObject(c.Param("object"), name, size)
		if !ok || sizeErr != nil || err != nil {
			detail(c, 401, "Storage capability invalid or expired")
			return
		}
		if !storageCORS(c, relation) {
			return
		}
		controller := http.NewResponseController(c.Writer)
		defer controller.SetReadDeadline(time.Time{})
		inactivity := time.Duration(min(settings.AdminUploadInactivity, 315360000)) * time.Second
		if inactivity <= 0 {
			inactivity = 120 * time.Second
		}
		c.Request.Body = http.MaxBytesReader(c.Writer, &uploadReader{c.Request.Body, controller, inactivity}, size)
		receipt, err := volume.OwnedUpload(c.Request.Context(), relation.ID, object, size, c.Request.Body, store.NodeAudit{Actor: relation.PeerID, RequestID: c.GetString("request_id"), TraceID: c.GetString("trace_id")})
		if err != nil {
			if errors.Is(err, store.ErrStorageCapacity) {
				detail(c, 507, "Follower storage is not writable or lacks capacity")
			} else if errors.Is(err, store.ErrNodeState) {
				detail(c, 409, "Storage reservation conflict")
			} else {
				uploadError(c, err)
			}
			return
		}
		c.JSON(200, receipt)
	})
	router.POST("/internal/v1/storage/:object/stat", func(c *gin.Context) {
		if !nodeHTTPS(c, settings, resolver) {
			return
		}
		body, ok := nodeBody(c)
		if !ok {
			return
		}
		path := c.Request.URL.EscapedPath()
		if c.Request.URL.RawQuery != "" {
			path += "?" + c.Request.URL.RawQuery
		}
		relation, err := control.Authenticate(c.Request.Context(), c.Request.Header, c.Request.Method, path, body, false, false)
		if err != nil {
			if errors.Is(err, node.ErrAuthentication) {
				detail(c, 401, "Invalid relationship authentication")
			} else {
				internalError(c, err)
			}
			return
		}
		if err = control.RequireFollower(c.Request.Context(), relation); err != nil {
			if errors.Is(err, store.ErrNodeState) {
				detail(c, 403, "Only a Follower reports storage objects")
			} else {
				internalError(c, err)
			}
			return
		}
		var value struct {
			Path string `json:"path"`
		}
		if controlJSON(body, &value) != nil {
			detail(c, 400, "Invalid storage stat request")
			return
		}
		receipt, err := volume.StorageStat(c.Request.Context(), c.Param("object"), value.Path)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) || errors.Is(err, media.ErrPath) {
				detail(c, 404, "Storage object not found")
			} else {
				internalError(c, err)
			}
			return
		}
		c.JSON(200, receipt)
	})
}
