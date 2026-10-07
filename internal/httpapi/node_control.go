package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func nodeHTTPS(c *gin.Context, settings config.Config, resolver *network.Resolver) bool {
	noStore(c)
	c.Header("Cache-Control", "no-store")
	identity := resolver.Resolve(c.Request)
	secure := c.Request.TLS != nil || (identity.FromTrustedProxy && len(c.Request.Header.Values("X-Forwarded-Proto")) == 1 && c.GetHeader("X-Forwarded-Proto") == "https")
	if !settings.TLSEnabled || !secure {
		detail(c, 403, "节点控制面仅允许已启用 TLS 的 HTTPS")
		return false
	}
	return true
}
func nodeBody(c *gin.Context) ([]byte, bool) {
	controller := http.NewResponseController(c.Writer)
	_ = controller.SetReadDeadline(time.Now().Add(10 * time.Second))
	defer controller.SetReadDeadline(time.Time{})
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, node.MaxControlBytes)
	body, err := io.ReadAll(c.Request.Body)
	if err != nil {
		var limit *http.MaxBytesError
		if errors.As(err, &limit) {
			detail(c, 413, "Control message too large")
		} else {
			detail(c, 400, "Invalid control message")
		}
		return nil, false
	}
	return body, true
}
func controlJSON(body []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var tail any
	if err := decoder.Decode(&tail); err != io.EOF {
		return errors.New("trailing control JSON")
	}
	return nil
}

// RegisterNodeControl separates small bounded control bodies from resource
// data-plane handlers. Authentication signs the exact raw body and query path.
func RegisterNodeControl(router *gin.Engine, settings config.Config, resolver *network.Resolver, service *node.Service) {
	router.POST("/internal/v1/pair", func(c *gin.Context) {
		if !nodeHTTPS(c, settings, resolver) {
			return
		}
		body, ok := nodeBody(c)
		if !ok {
			return
		}
		var value struct {
			Package    node.Envelope `json:"package"`
			Master     node.Envelope `json:"master"`
			ID         string        `json:"relationship_id"`
			Credential string        `json:"credential"`
		}
		if controlJSON(body, &value) != nil {
			detail(c, 409, "配对被拒绝：包无效、身份不匹配、已使用或已过期")
			return
		}
		ctx, cancel := context.WithTimeout(c.Request.Context(), 20*time.Second)
		defer cancel()
		if err := service.ConsumePair(ctx, value.Package, value.Master, value.ID, value.Credential); err != nil {
			detail(c, 409, "配对被拒绝：包无效、身份不匹配、已使用或已过期")
			return
		}
		c.JSON(200, gin.H{"state": "pending", "protocol": protocol.Version})
	})
	for _, route := range []string{"confirm", "revoke", "heartbeat"} {
		router.POST("/internal/v1/"+route, func(c *gin.Context) {
			if !nodeHTTPS(c, settings, resolver) {
				return
			}
			body, ok := nodeBody(c)
			if !ok {
				return
			}
			ctx, cancel := context.WithTimeout(c.Request.Context(), 20*time.Second)
			defer cancel()
			path := c.Request.URL.EscapedPath()
			if c.Request.URL.RawQuery != "" {
				path += "?" + c.Request.URL.RawQuery
			}
			relation, err := service.Authenticate(ctx, c.Request.Header, c.Request.Method, path, body, route != "heartbeat", route == "revoke")
			if err != nil {
				if errors.Is(err, node.ErrAuthentication) {
					detail(c, 401, "Invalid relationship authentication")
				} else {
					internalError(c, err)
				}
				return
			}
			var value map[string]any
			if len(body) == 0 {
				value = map[string]any{}
			} else if controlJSON(body, &value) != nil || value == nil {
				detail(c, 400, "Invalid control message")
				return
			}
			switch route {
			case "confirm":
				err = service.Confirm(ctx, relation)
			case "revoke":
				err = service.ReceiveRevocation(ctx, relation)
			case "heartbeat":
				var result map[string]any
				result, err = service.ReceiveHeartbeat(ctx, relation, value)
				if err != nil {
					if errors.Is(err, store.ErrNodeState) {
						detail(c, 409, "Node state conflict")
					} else {
						detail(c, 400, "Invalid heartbeat configuration")
					}
					return
				}
				c.JSON(200, result)
				return
			}
			if err != nil {
				if errors.Is(err, store.ErrNodeState) {
					detail(c, 409, "Node state conflict")
				} else {
					internalError(c, err)
				}
				return
			}
			state := "active"
			if route == "revoke" {
				state = "revoked"
			}
			c.JSON(200, gin.H{"state": state, "protocol": protocol.Version})
		})
	}
}
