package httpapi

import (
	"encoding/json"
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/media"
)

func (a *Admin) download(c *gin.Context) {
	var paths []string
	if json.Unmarshal([]byte(c.Query("paths")), &paths) != nil || len(paths) == 0 || len(paths) > a.settings.AdminMaxDownloadItems {
		detail(c, 400, "请选择合法下载对象")
		return
	}
	download, err := a.public.media.Download(c.Request.Context(), paths)
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	defer download.Close()
	source, _ := json.Marshal(paths)
	kind := "zip"
	if len(download.Items) == 1 && !download.Items[0].Directory {
		kind = "single_file"
	}
	if err := a.auth.Audit(c.Request.Context(), session(c).Hash, "download", string(source), "success", kind, len(paths), a.info(c)); err != nil {
		internalError(c, err)
		return
	}
	c.Header("Cache-Control", "private, no-store")
	if kind == "single_file" {
		name := download.Items[0].Path
		f, info, err := download.Open(name)
		if err != nil {
			mediaAdminError(c, err)
			return
		}
		defer f.Close()
		filename := media.DownloadFilename(name)
		c.Header("Content-Disposition", "attachment; filename*=UTF-8''"+media.QuotePath(filename))
		c.Header("Content-Type", "application/octet-stream")
		if a.settings.NginxMedia {
			c.Header("X-Accel-Redirect", "/_protected_media/"+media.QuotePath(name))
			c.Status(200)
			return
		}
		http.ServeContent(c.Writer, c.Request, filename, info.ModTime(), f)
		return
	}
	c.Header("Content-Type", "application/zip")
	c.Header("Content-Disposition", `attachment; filename="media-download.zip"`)
	c.Header("X-Accel-Buffering", "no")
	if err := download.ZIP(c.Writer); err != nil {
		slog.Error("media archive stream interrupted", "request_id", c.GetString("request_id"), "error", err)
		c.Abort()
	}
}
