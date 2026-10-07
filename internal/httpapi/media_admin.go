package httpapi

import (
	"encoding/json"
	"errors"
	"os"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/search"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (a *Admin) mutationAudit(c *gin.Context, action string, paths []string) store.AdminAudit {
	info := a.info(c)
	source, _ := json.Marshal(paths)
	return store.AdminAudit{SessionHash: session(c).Hash, Action: action, SourceSummary: string(source), ClientIP: info.IP, UserAgent: info.UserAgent, RequestID: info.RequestID, TraceID: info.TraceID}
}

func mediaAdminError(c *gin.Context, err error) {
	switch {
	case errors.Is(err, store.ErrNodeState):
		detail(c, 409, "节点状态或存储操作冲突")
	case errors.Is(err, store.ErrStorageCapacity):
		detail(c, 507, "存储空间不足")
	case errors.Is(err, media.ErrUnavailable):
		noStore(c)
		c.Header("Retry-After", "30")
		detail(c, 503, err.Error())
	case errors.Is(err, os.ErrExist):
		detail(c, 409, "目标名称或媒体身份已存在")
	case errors.Is(err, os.ErrNotExist), errors.Is(err, media.ErrCategory):
		detail(c, 404, "目录或媒体不存在")
	case errors.Is(err, media.ErrPath):
		detail(c, 400, "目录或媒体路径无效")
	case errors.Is(err, search.ErrQuery):
		detail(c, 400, err.Error())
	default:
		internalError(c, err)
	}
}

func (a *Admin) renameDirectory(c *gin.Context) {
	var body struct {
		Path    string `json:"path"`
		NewName string `json:"new_name"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	result, err := a.public.media.Rename(c.Request.Context(), body.Path, body.NewName, a.mutationAudit(c, "directory_rename", []string{body.Path}))
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, result)
}

func (a *Admin) tree(c *gin.Context) {
	value, err := a.public.media.Tree(c.Request.Context(), c.Query("path"))
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, value)
}

func (a *Admin) treeSearch(c *gin.Context) {
	query, ok := queryText(c, "q", 100)
	if !ok {
		return
	}
	value, err := a.public.media.Search(c.Request.Context(), query, c.Query("path"))
	if err != nil {
		if errors.Is(err, search.ErrQuery) {
			detail(c, 400, err.Error())
			return
		}
		if errors.Is(err, os.ErrNotExist) || errors.Is(err, media.ErrCategory) || errors.Is(err, media.ErrPath) {
			detail(c, 400, "搜索目录不在受支持的媒体层级内或不存在")
			return
		}
		internalError(c, err)
		return
	}
	c.JSON(200, value)
}

func (a *Admin) mediaPriorities(c *gin.Context) {
	page, pageSize := 1, 100
	for name, target := range map[string]*int{"page": &page, "page_size": &pageSize} {
		if value, exists := c.GetQuery(name); exists {
			parsed, err := strconv.Atoi(value)
			if err != nil || parsed < 1 || (name == "page_size" && parsed > 100) {
				invalid(c, "query", name)
				return
			}
			*target = parsed
		}
	}
	kind := c.Query("media_type")
	if kind != "" && kind != "audio" && kind != "video" {
		invalid(c, "query", "media_type")
		return
	}
	query, scope := c.Query("q"), c.Query("path")
	if utf8.RuneCountInString(query) > 100 || utf8.RuneCountInString(scope) > 1024 {
		invalid(c, "query", "payload")
		return
	}
	value, err := a.public.media.Priorities(c.Request.Context(), scope, query, kind, page, pageSize)
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, value)
}

func (a *Admin) hide(c *gin.Context) {
	var body struct {
		Paths  []string        `json:"paths"`
		Hidden json.RawMessage `json:"hidden"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	if len(body.Paths) == 0 || len(body.Paths) > a.settings.AdminMaxBatchFiles {
		detail(c, 400, "隐藏参数无效")
		return
	}
	hide := true
	if body.Hidden != nil {
		if string(body.Hidden) == "null" || json.Unmarshal(body.Hidden, &hide) != nil {
			detail(c, 400, "隐藏参数无效")
			return
		}
	}
	action := "hide"
	if !hide {
		action = "unhide"
	}
	if err := a.public.media.Hide(c.Request.Context(), body.Paths, hide, a.mutationAudit(c, action, body.Paths)); err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, gin.H{"status": "ok", "hidden": hide})
}

func (a *Admin) delete(c *gin.Context) {
	var body struct {
		Paths []string `json:"paths"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	if len(body.Paths) == 0 || len(body.Paths) > a.settings.AdminMaxBatchFiles {
		detail(c, 400, "请选择合法对象")
		return
	}
	role, roleErr := a.public.media.BusinessRole(c.Request.Context())
	if roleErr != nil {
		internalError(c, roleErr)
		return
	}
	global := role == "Master"
	for _, p := range body.Paths {
		if strings.HasPrefix(p, "lyrics/") {
			global = false
		}
	}
	if global {
		result, err := a.public.media.DeleteGlobal(c.Request.Context(), body.Paths, a.mutationAudit(c, "global-media-delete", body.Paths))
		if err != nil {
			mediaAdminError(c, err)
			return
		}
		c.JSON(200, result)
		return
	}
	count, err := a.public.media.Delete(c.Request.Context(), body.Paths, a.mutationAudit(c, "delete", body.Paths))
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, gin.H{"deleted": count})
}

func (a *Admin) directoryPriorities(c *gin.Context) {
	items, err := a.public.media.DirectoryPreferences(c.Request.Context(), c.Query("scope"))
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, gin.H{"items": items, "minimum": -7, "maximum": 500})
}

func (a *Admin) directoryPriority(c *gin.Context) {
	var body struct {
		Path  string `json:"path"`
		Value *int   `json:"value"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	if !validPriority(c, body.Path, body.Value) {
		return
	}
	value, err := a.public.media.Preference(c.Request.Context(), body.Path, *body.Value, true, a.mutationAudit(c, "directory_priority", []string{body.Path}))
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, gin.H{"directory_path": value.Path, "preference": value.Preference})
}

func validPriority(c *gin.Context, path string, value *int) bool {
	if strings.TrimSpace(path) == "" || !utf8.ValidString(path) || utf8.RuneCountInString(path) > 1024 || value == nil || *value < -7 || *value > 500 {
		invalid(c, "body", "payload")
		return false
	}
	return true
}

func (a *Admin) mediaPriority(c *gin.Context) {
	var body struct {
		Path     string `json:"media_path"`
		Resource string `json:"resource_id"`
		Value    *int   `json:"value"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	if !validPriority(c, body.Path, body.Value) {
		return
	}
	if body.Resource != "" {
		detail(c, 503, "Federated media runtime is not enabled on this node")
		return
	}
	value, err := a.public.media.Preference(c.Request.Context(), body.Path, *body.Value, false, a.mutationAudit(c, "media_priority", []string{body.Path}))
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, gin.H{"media_id": value.MediaID, "play_score": value.PlayScore, "preference": value.Preference})
}
