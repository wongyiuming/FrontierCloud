package httpapi

import (
	"github.com/gin-gonic/gin"
	"net/http"
	"time"
	"unicode/utf8"
)

func (a *Admin) storagePool(c *gin.Context) {
	result, err := a.public.media.StoragePool(c.Request.Context())
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, result)
}
func (a *Admin) reserveUpload(c *gin.Context) {
	var body struct {
		Site     string `json:"site_type"`
		Target   string `json:"target_dir"`
		Relative string `json:"relative_path"`
		Filename string `json:"filename"`
		Bytes    int64  `json:"size_bytes"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	if (body.Site != "primary" && body.Site != "direct" && body.Site != "relay") || utf8.RuneCountInString(body.Target) > 1024 || utf8.RuneCountInString(body.Relative) > 1024 || body.Filename == "" || utf8.RuneCountInString(body.Filename) > 255 || body.Bytes <= 0 {
		invalid(c, "body", "upload")
		return
	}
	if body.Bytes > a.settings.AdminMaxUploadBytes {
		detail(c, 413, "文件超过单文件上传限制")
		return
	}
	result, err := a.public.media.ReserveMasterUpload(c.Request.Context(), body.Filename, body.Target, body.Relative, body.Site, body.Bytes, a.settings.AdminMaxFilenameLength, a.mutationAudit(c, "upload-reserved", []string{body.Filename}))
	if err != nil {
		uploadError(c, err)
		return
	}
	c.JSON(200, result)
}
func (a *Admin) uploadBytes(c *gin.Context) {
	controller := http.NewResponseController(c.Writer)
	defer controller.SetReadDeadline(time.Time{})
	c.Request.Body = http.MaxBytesReader(c.Writer, &uploadReader{c.Request.Body, controller, time.Duration(min(a.settings.AdminUploadInactivity, 315360000)) * time.Second}, a.settings.AdminMaxUploadBytes)
	result, err := a.public.media.UploadMasterBytes(c.Request.Context(), c.Param("upload"), c.Request.Body, a.mutationAudit(c, "upload-finalized", []string{c.Param("upload")}))
	if err != nil {
		uploadError(c, err)
		return
	}
	c.JSON(200, result)
}
func (a *Admin) finalizeUpload(c *gin.Context) {
	result, err := a.public.media.FinalizeMasterUpload(c.Request.Context(), c.Param("upload"), a.mutationAudit(c, "upload-finalized", []string{c.Param("upload")}))
	if err != nil {
		uploadError(c, err)
		return
	}
	c.JSON(200, result)
}
func (a *Admin) cancelUpload(c *gin.Context) {
	if err := a.public.media.CancelMasterUpload(c.Request.Context(), c.Param("upload"), a.mutationAudit(c, "upload-cancelled", []string{c.Param("upload")})); err != nil {
		uploadError(c, err)
		return
	}
	c.JSON(200, gin.H{"status": "cancelled"})
}
