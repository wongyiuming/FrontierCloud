package httpapi

import (
	"bytes"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/brand"
)

func (p *Public) brandLogo(c *gin.Context) {
	logo, err := p.brand.Effective(c.Param("kind"))
	if err != nil {
		if errors.Is(err, brand.ErrKind) {
			detail(c, 404, err.Error())
		} else {
			internalError(c, err)
		}
		return
	}
	if c.Query("v") != logo.Version {
		c.Header("Cache-Control", "public, max-age=10, stale-while-revalidate=60")
		c.Redirect(307, logo.URL)
		return
	}
	c.Header("Cache-Control", "public, max-age=31536000, immutable")
	c.Header("ETag", `"brand-`+logo.Version+`"`)
	c.Header("Content-Type", logo.ContentType)
	http.ServeContent(c.Writer, c.Request, logo.Filename, logo.Modified, bytes.NewReader(logo.Payload))
}
func (a *Admin) brandStatus(c *gin.Context) {
	items := []brand.Logo{}
	for _, kind := range []string{"entertainment", "media", "music"} {
		logo, err := a.public.brand.Effective(kind)
		if err != nil {
			internalError(c, err)
			return
		}
		items = append(items, logo)
	}
	c.JSON(200, gin.H{"items": items, "max_upload_bytes": brand.MaxBytes})
}
func (a *Admin) brandAudit(c *gin.Context, action, source, detail string) {
	if err := a.auth.Audit(c.Request.Context(), session(c).Hash, action, source, "success", detail, 1, a.info(c)); err != nil {
		slog.Error("brand audit write failed", "action", action, "error", err)
	}
}
func (a *Admin) brandDownload(c *gin.Context) {
	logo, err := a.public.brand.Effective(c.Param("kind"))
	if err != nil {
		if errors.Is(err, brand.ErrKind) {
			detail(c, 404, err.Error())
		} else {
			internalError(c, err)
		}
		return
	}
	a.brandAudit(c, "brand-logo-download", logo.Kind, logo.Source)
	c.Header("Cache-Control", "private, no-store")
	c.Header("Content-Type", logo.ContentType)
	c.Header("Content-Disposition", `attachment; filename="`+logo.Filename+`"`)
	http.ServeContent(c.Writer, c.Request, logo.Filename, logo.Modified, bytes.NewReader(logo.Payload))
}
func (a *Admin) brandUpload(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, brand.MaxBytes+64*1024)
	if err := c.Request.ParseMultipartForm(brand.MaxBytes + 64*1024); err != nil {
		var sizeError *http.MaxBytesError
		if errors.As(err, &sizeError) {
			detail(c, 413, brand.ErrSize.Error())
		} else {
			invalid(c, "body", "file")
		}
		return
	}
	defer c.Request.MultipartForm.RemoveAll()
	f, _, err := c.Request.FormFile("file")
	if err != nil {
		invalid(c, "body", "file")
		return
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, brand.MaxBytes+1))
	if err != nil {
		internalError(c, err)
		return
	}
	logo, err := a.public.brand.Upload(c.Param("kind"), data)
	if err != nil {
		status := 400
		if errors.Is(err, brand.ErrSize) {
			status = 413
		}
		if errors.Is(err, brand.ErrKind) || errors.Is(err, brand.ErrImage) || errors.Is(err, brand.ErrSize) || err.Error() == "Logo 文件为空" {
			detail(c, status, err.Error())
		} else {
			internalError(c, err)
		}
		return
	}
	a.brandAudit(c, "brand-logo-upload", logo.Kind, logo.ContentType+":"+strconv.Itoa(len(data)))
	c.JSON(200, logo)
}
func (a *Admin) brandDelete(c *gin.Context) {
	logo, changed, err := a.public.brand.Delete(c.Param("kind"))
	if err != nil {
		if errors.Is(err, brand.ErrKind) {
			detail(c, 400, err.Error())
		} else {
			internalError(c, err)
		}
		return
	}
	result := "already-default"
	if changed {
		result = "restored-default"
	}
	a.brandAudit(c, "brand-logo-delete", logo.Kind, result)
	c.JSON(200, logo)
}
