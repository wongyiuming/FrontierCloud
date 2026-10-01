package httpapi

import (
	"context"
	"encoding/base64"
	"errors"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func RegisterNodeBackups(router *gin.Engine, settings config.Config, resolver *network.Resolver, control *node.Service, repository store.BackupRepository) {
	for _, operation := range []string{"begin", "chunk", "commit", "abort"} {
		router.POST("/internal/v1/backup/"+operation, func(c *gin.Context) {
			if !nodeHTTPS(c, settings, resolver) {
				return
			}
			body, ok := nodeBody(c)
			if !ok {
				return
			}
			ctx, cancel := context.WithTimeout(c.Request.Context(), 60*time.Second)
			defer cancel()
			path := c.Request.URL.EscapedPath()
			if c.Request.URL.RawQuery != "" {
				path += "?" + c.Request.URL.RawQuery
			}
			rel, err := control.Authenticate(ctx, c.Request.Header, c.Request.Method, path, body, false, false)
			if err != nil {
				if errors.Is(err, node.ErrAuthentication) {
					detail(c, 401, "Invalid relationship authentication")
				} else {
					internalError(c, err)
				}
				return
			}
			if err := control.RequireFollower(ctx, rel); err != nil {
				if errors.Is(err, store.ErrNodeState) {
					detail(c, 403, "Only the active upstream may store Follower backups")
				} else {
					internalError(c, err)
				}
				return
			}
			var value struct {
				Generation int64   `json:"generation"`
				Index      *int    `json:"chunk_index"`
				Chunk      *string `json:"chunk"`
				Checksum   string  `json:"checksum"`
			}
			if controlJSON(body, &value) != nil || value.Generation <= 0 {
				detail(c, 400, "Invalid backup message")
				return
			}
			audit := store.NodeAudit{Actor: rel.PeerID, RequestID: c.GetString("request_id"), TraceID: c.GetString("trace_id")}
			switch operation {
			case "begin":
				err = repository.BeginBackup(ctx, rel.ID, value.Generation, audit)
				if err == nil {
					c.JSON(200, gin.H{"status": "receiving"})
					return
				}
			case "chunk":
				if value.Index == nil || value.Chunk == nil || len(*value.Chunk) > base64.StdEncoding.EncodedLen(store.MaxBackupChunk) || strings.ContainsAny(*value.Chunk, "\r\n") {
					detail(c, 400, "Invalid backup chunk")
					return
				}
				chunk, decodeErr := base64.StdEncoding.Strict().DecodeString(*value.Chunk)
				if decodeErr != nil || len(chunk) > store.MaxBackupChunk {
					detail(c, 400, "Invalid backup chunk")
					return
				}
				err = repository.AppendBackup(ctx, rel.ID, value.Generation, *value.Index, chunk)
				if err == nil {
					c.JSON(200, gin.H{"status": "receiving", "bytes": len(chunk)})
					return
				}
			case "commit":
				var manifest store.BackupManifest
				manifest, err = repository.CommitBackup(ctx, rel.ID, value.Generation, value.Checksum, audit)
				if err == nil {
					c.JSON(200, gin.H{"status": "ready", "bytes": manifest.Bytes})
					return
				}
			case "abort":
				var result store.BackupAbort
				result, err = repository.AbortBackups(ctx, rel.ID, value.Generation, audit)
				if err == nil {
					c.JSON(200, result)
					return
				}
			}
			if errors.Is(err, store.ErrBackupState) {
				detail(c, 400, "Invalid or incomplete business backup")
			} else if errors.Is(err, store.ErrNodeState) {
				detail(c, 409, "Backup relationship or node state changed")
			} else {
				internalError(c, err)
			}
		})
	}
}
