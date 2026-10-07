package httpapi

import (
	"encoding/json"
	"errors"
	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/recording"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"net/http"
	"net/url"
	"time"
)

func recordingError(c *gin.Context, e error) {
	switch {
	case errors.Is(e, store.ErrRecordingMissing):
		detail(c, 404, e.Error())
	case errors.Is(e, store.ErrUserMissing):
		detail(c, 404, e.Error())
	case errors.Is(e, store.ErrUserBlocked):
		detail(c, 403, e.Error())
	case errors.Is(e, recording.ErrSize):
		detail(c, 400, e.Error())
	case errors.Is(e, store.ErrRecordingQuota):
		detail(c, 413, e.Error())
	case errors.Is(e, store.ErrStorageCapacity):
		detail(c, 507, e.Error())
	case errors.Is(e, filelease.ErrBusy), errors.Is(e, store.ErrRecordingState), errors.Is(e, store.ErrNodeState):
		detail(c, 409, "录音操作正在进行或状态已变化")
	case errors.Is(e, recording.ErrRecovery):
		detail(c, 503, "录音存储正在恢复")
	default:
		var maxBytes *http.MaxBytesError
		if errors.As(e, &maxBytes) {
			detail(c, 413, "录音超过预留大小")
		} else {
			internalError(c, e)
		}
	}
}
func recordingCORS(c *gin.Context, rel store.Relationship) bool {
	c.Header("Vary", "Origin")
	origins := c.Request.Header.Values("Origin")
	if len(origins) > 1 || len(origins) == 1 && origins[0] != rel.Endpoint {
		detail(c, 403, "Unpaired recording origin")
		return false
	}
	if len(origins) == 1 {
		c.Header("Access-Control-Allow-Origin", origins[0])
		c.Header("Access-Control-Allow-Methods", "GET, HEAD, PUT, OPTIONS")
		c.Header("Access-Control-Allow-Headers", "Content-Type, X-Recording-Capability, Range, If-Range")
		c.Header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges, ETag, Last-Modified, Content-Disposition")
	}
	return true
}
func boundedRecordingBody(c *gin.Context, settings config.Config, size int64) func() {
	controller := http.NewResponseController(c.Writer)
	inactivity := time.Duration(min(settings.AdminUploadInactivity, 315360000)) * time.Second
	if inactivity <= 0 {
		inactivity = 120 * time.Second
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, &uploadReader{c.Request.Body, controller, inactivity}, size)
	return func() { controller.SetReadDeadline(time.Time{}) }
}
func serveRecording(c *gin.Context, volume *recording.Storage, relationship, user, id, contentType, filename string, download bool) {
	file, info, release, e := volume.Open(c.Request.Context(), relationship, user, id)
	if e != nil {
		recordingError(c, e)
		return
	}
	defer release()
	noStore(c)
	c.Header("Content-Type", contentType)
	disposition := "inline"
	if download {
		disposition = "attachment"
	}
	c.Header("Content-Disposition", disposition+"; filename*=UTF-8''"+url.PathEscape(filename))
	c.Header("X-Content-Type-Options", "nosniff")
	http.ServeContent(c.Writer, c.Request, filename, info.ModTime(), file)
}
func RegisterNodeRecordings(router *gin.Engine, settings config.Config, resolver *network.Resolver, control *node.Service, volume *recording.Storage) {
	router.POST("/internal/v1/recordings/users/:user/delete", func(c *gin.Context) {
		if !nodeHTTPS(c, settings, resolver) {
			return
		}
		body, ok := nodeBody(c)
		if !ok {
			return
		}
		rel, e := control.Authenticate(c.Request.Context(), c.Request.Header, c.Request.Method, c.Request.URL.RequestURI(), body, false, false)
		if e != nil {
			if errors.Is(e, node.ErrAuthentication) {
				detail(c, 401, "Invalid relationship authentication")
			} else {
				internalError(c, e)
			}
			return
		}
		if e = control.RequireFollower(c.Request.Context(), rel); e != nil {
			recordingError(c, e)
			return
		}
		removed, e := volume.DeleteUser(c.Request.Context(), rel.ID, c.Param("user"), store.NodeAudit{Actor: rel.PeerID, RequestID: c.GetString("request_id"), TraceID: c.GetString("trace_id")})
		if e != nil {
			recordingError(c, e)
			return
		}
		c.JSON(200, gin.H{"status": "deleted", "removed_bytes": removed})
	})
	router.OPTIONS("/internal/v1/recordings/:recording", func(c *gin.Context) {
		if !nodeHTTPS(c, settings, resolver) {
			return
		}
		origins := c.Request.Header.Values("Origin")
		if len(origins) != 1 {
			detail(c, 403, "Unpaired recording origin")
			return
		}
		rel, e := control.FollowerOrigin(c.Request.Context(), origins[0])
		if e != nil {
			if errors.Is(e, node.ErrCapability) {
				detail(c, 403, "Unpaired recording origin")
			} else {
				internalError(c, e)
			}
			return
		}
		if recordingCORS(c, rel) {
			c.Status(200)
		}
	})
	serve := func(c *gin.Context) {
		if !nodeHTTPS(c, settings, resolver) {
			return
		}
		token, ok := capabilityInput(c, "X-Recording-Capability")
		if !ok {
			detail(c, 401, "Invalid recording capability")
			return
		}
		operation := "read"
		if c.Request.Method == "PUT" {
			operation = "upload"
		}
		rel, value, e := control.VerifyOwnedRecording(c.Request.Context(), token, c.Param("recording"), operation)
		if e != nil {
			if errors.Is(e, node.ErrCapability) {
				detail(c, 401, "Invalid recording capability")
			} else {
				internalError(c, e)
			}
			return
		}
		if !recordingCORS(c, rel) {
			return
		}
		user, _ := value["u"].(string)
		filename, _ := value["name"].(string)
		ct, _ := value["ct"].(string)
		if c.Request.Method != "PUT" {
			op, _ := value["op"].(string)
			serveRecording(c, volume, rel.ID, user, c.Param("recording"), ct, filename, op == "download")
			return
		}
		number, ok := value["size"].(json.Number)
		size, se := number.Int64()
		if !ok || se != nil || size <= 0 {
			detail(c, 401, "Invalid recording capability")
			return
		}
		unlock, e := volume.Lock(c.Param("recording"))
		if e != nil {
			recordingError(c, e)
			return
		}
		defer unlock()
		defer boundedRecordingBody(c, settings, size)()
		receipt, e := volume.Upload(c.Request.Context(), rel.ID, store.Recording{ID: c.Param("recording"), UserID: user, Filename: filename, ContentType: ct, Bytes: size}, c.Request.Body, store.NodeAudit{Actor: rel.PeerID, RequestID: c.GetString("request_id"), TraceID: c.GetString("trace_id")})
		if e != nil {
			recordingError(c, e)
			return
		}
		c.JSON(200, receipt)
	}
	router.GET("/internal/v1/recordings/:recording", serve)
	router.HEAD("/internal/v1/recordings/:recording", serve)
	router.PUT("/internal/v1/recordings/:recording", serve)
	for _, op := range []string{"stat", "delete"} {
		router.POST("/internal/v1/recordings/:recording/"+op, func(c *gin.Context) {
			if !nodeHTTPS(c, settings, resolver) {
				return
			}
			body, ok := nodeBody(c)
			if !ok {
				return
			}
			rel, e := control.Authenticate(c.Request.Context(), c.Request.Header, c.Request.Method, c.Request.URL.RequestURI(), body, false, false)
			if e != nil {
				if errors.Is(e, node.ErrAuthentication) {
					detail(c, 401, "Invalid relationship authentication")
				} else {
					internalError(c, e)
				}
				return
			}
			if e = control.RequireFollower(c.Request.Context(), rel); e != nil {
				recordingError(c, e)
				return
			}
			var value struct {
				UserID string `json:"user_id"`
			}
			if controlJSON(body, &value) != nil || !node.ValidIdentifier(value.UserID) || !node.ValidIdentifier(c.Param("recording")) {
				detail(c, 400, "Invalid recording owner")
				return
			}
			if op == "stat" {
				receipt, e := volume.Stat(c.Request.Context(), rel.ID, value.UserID, c.Param("recording"))
				if e != nil {
					recordingError(c, e)
					return
				}
				c.JSON(200, receipt)
				return
			}
			unlock, e := volume.Lock(c.Param("recording"))
			if e != nil {
				recordingError(c, e)
				return
			}
			defer unlock()
			e = volume.Delete(c.Request.Context(), rel.ID, value.UserID, c.Param("recording"), store.KaraokeAudit{}, store.NodeAudit{Actor: rel.PeerID, RequestID: c.GetString("request_id"), TraceID: c.GetString("trace_id")})
			if e != nil {
				recordingError(c, e)
				return
			}
			c.JSON(200, gin.H{"status": "deleted"})
		})
	}
}
