package httpapi

import (
	"errors"
	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/recording"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"strings"
	"unicode/utf8"
)

func RegisterKaraokeRecordings(router *gin.Engine, accounts *KaraokeAccounts, manager *recording.Manager, volume *recording.Storage) {
	accounts.service.ConfigureRecordings(manager.RecoverUser)
	g := router.Group("/api/v1/karaoke/account/recordings", func(c *gin.Context) {
		noStore(c)
		if accounts.public.settings.TLSEnabled && !accounts.resolver.SecureAdmin(c.Request) {
			detail(c, 426, "账号访问需要 HTTPS")
			c.Abort()
			return
		}
		mutation := c.Request.Method != "GET" && c.Request.Method != "HEAD"
		user, ok := accounts.current(c, mutation, false)
		if !ok {
			c.Abort()
			return
		}
		c.Set("recording-user", user)
		c.Next()
	})
	user := func(c *gin.Context) *store.KaraokeUser {
		value, _ := c.Get("recording-user")
		return value.(*store.KaraokeUser)
	}
	audit := func(c *gin.Context) store.KaraokeAudit {
		info := accounts.info(c)
		return store.KaraokeAudit{UserID: user(c).ID, IP: info.IP, RequestID: info.RequestID, TraceID: info.TraceID}
	}
	g.POST("/ticket", func(c *gin.Context) {
		var value struct {
			Bytes       int64   `json:"size_bytes"`
			ContentType string  `json:"content_type"`
			Media       *string `json:"media"`
			Title       *string `json:"title"`
		}
		if !decodeAdmin(c, &value) {
			return
		}
		ct := strings.ToLower(strings.Split(value.ContentType, ";")[0])
		if !store.RecordingContentType(ct) {
			detail(c, 415, "只允许上传受支持的录音音频")
			return
		}
		if value.Bytes < 1 || value.Bytes > store.MaxRecordingBytes || len(value.ContentType) > 96 || value.Title != nil && utf8.RuneCountInString(*value.Title) > 255 {
			invalid(c, "body", "size_bytes")
			return
		}
		metadata := store.RecordingMetadata{Title: "卡拉OK录音", Lyrics: []store.RecordingLyric{}}
		if value.Title != nil && strings.TrimSpace(*value.Title) != "" {
			metadata.Title = strings.TrimSpace(*value.Title)
		}
		if value.Media != nil {
			resolved, e := accounts.public.media.KaraokeMetadata(c.Request.Context(), *value.Media)
			if e != nil {
				detail(c, 404, "Karaoke media not found")
				return
			}
			metadata = resolved
			if value.Title != nil && strings.TrimSpace(*value.Title) != "" {
				metadata.Title = strings.TrimSpace(*value.Title)
			}
		}
		ticket, e := manager.Ticket(c.Request.Context(), *user(c), value.Bytes, ct, metadata, audit(c))
		if e != nil {
			recordingError(c, e)
			return
		}
		c.JSON(200, ticket)
	})
	g.PUT("/:recording/content", func(c *gin.Context) {
		v, e := manager.PendingUpload(c.Request.Context(), user(c).ID, c.Param("recording"))
		if e != nil {
			recordingError(c, e)
			return
		}
		defer boundedRecordingBody(c, accounts.public.settings, v.Bytes)()
		receipt, e := manager.Upload(c.Request.Context(), user(c).ID, v.ID, c.Request.Body, audit(c))
		if e != nil {
			recordingError(c, e)
			return
		}
		c.JSON(200, receipt)
	})
	g.POST("/:recording/finalize", func(c *gin.Context) {
		if e := manager.Finalize(c.Request.Context(), user(c).ID, c.Param("recording"), audit(c)); e != nil {
			recordingError(c, e)
			return
		}
		c.JSON(200, gin.H{"status": "ready", "recording_id": c.Param("recording")})
	})
	g.GET("", func(c *gin.Context) {
		items, e := manager.List(c.Request.Context(), user(c).ID)
		if e != nil {
			recordingError(c, e)
			return
		}
		c.JSON(200, gin.H{"items": items})
	})
	for _, suffix := range []string{"", "/pending"} {
		g.DELETE("/:recording"+suffix, func(c *gin.Context) {
			a := audit(c)
			a.Action = "recording-delete"
			status := "deleted"
			if suffix != "" {
				a.Action = "recording-cancel"
				status = "cancelled"
			}
			if e := manager.Delete(c.Request.Context(), user(c).ID, c.Param("recording"), suffix != "", a); e != nil {
				recordingError(c, e)
				return
			}
			c.JSON(200, gin.H{"status": status})
		})
	}
	serve := func(download bool) gin.HandlerFunc {
		return func(c *gin.Context) {
			p, target, e := manager.Delivery(c.Request.Context(), user(c).ID, c.Param("recording"), download, accounts.public.settings.NginxMedia)
			if e != nil {
				if errors.Is(e, recording.ErrUnavailable) {
					detail(c, 503, e.Error())
				} else {
					recordingError(c, e)
				}
				return
			}
			if target != "" {
				if strings.HasPrefix(target, "https://") {
					c.Header("Referrer-Policy", "no-referrer")
					c.Redirect(307, target)
				} else {
					c.Header("X-Accel-Redirect", target)
					c.Status(200)
				}
				return
			}
			serveRecording(c, volume, p.Member.ID, user(c).ID, p.Recording.ID, p.Recording.ContentType, p.Recording.Filename, download)
		}
	}
	for _, method := range []string{"GET", "HEAD"} {
		g.Handle(method, "/:recording/stream", serve(false))
		g.Handle(method, "/:recording/download", serve(true))
	}
}
