package httpapi

import (
	"errors"
	"io"
	"math"
	"net"
	"net/http"
	"os"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/media"
)

type uploadReader struct {
	reader     io.ReadCloser
	control    *http.ResponseController
	inactivity time.Duration
}

func (r *uploadReader) Close() error { return r.reader.Close() }

func (r *uploadReader) Read(buffer []byte) (int, error) {
	if err := r.control.SetReadDeadline(time.Now().Add(r.inactivity)); err != nil && !errors.Is(err, http.ErrNotSupported) {
		return 0, err
	}
	return r.reader.Read(buffer)
}

func (a *Admin) upload(c *gin.Context, lyric bool) {
	role, err := a.public.media.BusinessRole(c.Request.Context())
	if err != nil {
		internalError(c, err)
		return
	}
	if role == "Follower" || !lyric && role == "Master" {
		detail(c, 409, "Master 媒体上传必须使用存储池会话；Follower 仅接受节点存储接口")
		return
	}
	maximum := a.settings.AdminMaxUploadBytes
	if lyric {
		maximum = media.MaxLyricUploadBytes
	}
	control := http.NewResponseController(c.Writer)
	defer control.SetReadDeadline(time.Time{})
	limit := maximum
	if limit <= math.MaxInt64-65536 {
		limit += 65536
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, &uploadReader{c.Request.Body, control, time.Duration(min(a.settings.AdminUploadInactivity, 315360000)) * time.Second}, limit)
	reader, err := c.Request.MultipartReader()
	if err != nil {
		invalid(c, "body", "file")
		return
	}
	fields := map[string]string{}
	filename := ""
	var staged *media.Stage
	defer func() {
		if staged != nil {
			staged.Close()
		}
	}()
	for count := 0; ; count++ {
		if count > 16 {
			c.Request.Body.Close()
			detail(c, 400, "上传表单字段过多")
			return
		}
		part, err := reader.NextPart()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			uploadError(c, err)
			return
		}
		name := part.FormName()
		if name == "file" && part.FileName() != "" {
			if staged != nil {
				c.Request.Body.Close()
				detail(c, 400, "每个请求只允许一个上传文件")
				return
			}
			filename = part.FileName()
			staged, err = a.public.media.Stage(c.Request.Context(), part, maximum)
			if err != nil {
				c.Request.Body.Close()
				uploadError(c, err)
				return
			}
			part.Close()
			continue
		}
		if name == "file" {
			c.Request.Body.Close()
			invalid(c, "body", "file")
			return
		}
		value, err := io.ReadAll(io.LimitReader(part, 4097))
		if err != nil {
			c.Request.Body.Close()
			uploadError(c, err)
			return
		}
		if len(value) > 4096 {
			c.Request.Body.Close()
			detail(c, 400, "上传表单字段过长")
			return
		}
		part.Close()
		if _, exists := fields[name]; exists {
			detail(c, 400, "上传表单字段重复")
			return
		}
		fields[name] = string(value)
	}
	if staged == nil {
		invalid(c, "body", "file")
		return
	}
	action := "upload_item"
	if lyric {
		action = "upload_lyric"
	}
	name, err := a.public.media.Publish(c.Request.Context(), staged, filename, fields["target_dir"], fields["relative_path"], lyric, a.settings.AdminMaxFilenameLength, a.mutationAudit(c, action, []string{filename}))
	if err != nil {
		uploadError(c, err)
		return
	}
	c.JSON(200, gin.H{"path": name})
}

func uploadError(c *gin.Context, err error) {
	var size *http.MaxBytesError
	var timeout net.Error
	switch {
	case errors.Is(err, media.ErrUploadSize), errors.As(err, &size):
		detail(c, 413, media.ErrUploadSize.Error())
	case errors.Is(err, os.ErrExist):
		detail(c, 409, "上传失败，目标位置已存在同名文件")
	case errors.Is(err, media.ErrSignature):
		detail(c, 400, err.Error())
	case errors.Is(err, media.ErrLayout):
		detail(c, 409, err.Error())
	case errors.Is(err, io.ErrUnexpectedEOF):
		detail(c, 400, "上传未完成")
	case errors.As(err, &timeout) && timeout.Timeout():
		detail(c, 408, "上传超过无活动超时")
	default:
		mediaAdminError(c, err)
	}
}
