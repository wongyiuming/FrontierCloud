package httpapi

import (
	"errors"
	"net/http"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/karaoke"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type KaraokeAccounts struct {
	service  *karaoke.Service
	public   *Public
	admin    *Admin
	resolver *network.Resolver
}

func RegisterKaraokeAccounts(router *gin.Engine, service *karaoke.Service, public *Public, admin *Admin, resolver *network.Resolver) *KaraokeAccounts {
	a := &KaraokeAccounts{service, public, admin, resolver}
	g := router.Group("/api/v1/karaoke/account", func(c *gin.Context) {
		noStore(c)
		if public.settings.TLSEnabled && !resolver.SecureAdmin(c.Request) {
			detail(c, 426, "账号访问需要 HTTPS")
			c.Abort()
			return
		}
		c.Next()
	})
	g.GET("/captcha", a.captcha)
	g.GET("/captcha/:challenge", a.captchaImage)
	g.POST("/register", func(c *gin.Context) { a.auth(c, true) })
	g.POST("/login", func(c *gin.Context) { a.auth(c, false) })
	g.GET("/status", a.status)
	g.POST("/logout", a.logout)
	g.POST("/password", a.password)
	g.DELETE("", a.deleteAccount)
	if admin != nil {
		users := router.Group("/api/v1/media/admin/users", admin.transport, admin.authenticate)
		users.GET("", a.listUsers)
		users.POST("/:user", a.mutateUser)
	}
	return a
}
func karaokeError(c *gin.Context, err error) {
	var rejected *karaoke.Error
	if errors.As(err, &rejected) {
		if rejected.Captcha {
			c.Header("X-Captcha-Required", "1")
		} else if rejected.Status == 401 {
			c.Header("X-Captcha-Required", "0")
		}
		detail(c, rejected.Status, rejected.Detail)
		return
	}
	internalError(c, err)
}
func userJSON(v store.KaraokeUser) gin.H {
	return gin.H{"user_id": v.ID, "username": v.Username, "status": v.Status, "quota_bytes": v.Quota, "used_bytes": v.Used}
}
func (a *KaraokeAccounts) info(c *gin.Context) karaoke.RequestInfo {
	return karaoke.RequestInfo{IP: a.resolver.Resolve(c.Request).IP, RequestID: c.GetString("request_id"), TraceID: c.GetString("trace_id")}
}
func (a *KaraokeAccounts) cookieNames() (string, string) {
	prefix := ""
	if a.public.settings.TLSEnabled {
		prefix = "__Host-"
	}
	return prefix + "karaoke_session", prefix + "karaoke_csrf"
}
func (a *KaraokeAccounts) cookies(c *gin.Context, s karaoke.Session, clear bool) {
	name, csrf := a.cookieNames()
	age := karaoke.SessionTTL
	if clear {
		age = -1
	}
	for _, v := range []struct {
		name, value string
		private     bool
	}{{name, s.Token, true}, {csrf, s.CSRF, false}} {
		http.SetCookie(c.Writer, &http.Cookie{Name: v.name, Value: v.value, Path: "/", MaxAge: age, HttpOnly: v.private, Secure: a.public.settings.TLSEnabled, SameSite: http.SameSiteLaxMode})
	}
}
func (a *KaraokeAccounts) current(c *gin.Context, mutation, optional bool) (*store.KaraokeUser, bool) {
	name, csrf := a.cookieNames()
	token, _ := c.Cookie(name)
	cookie, _ := c.Cookie(csrf)
	if mutation && len(c.Request.Header.Values("X-Karaoke-CSRF")) > 1 {
		detail(c, 403, "卡拉OK请求校验失败")
		return nil, false
	}
	user, err := a.service.CurrentUser(c.Request.Context(), token, cookie, c.GetHeader("X-Karaoke-CSRF"), mutation, optional)
	if err != nil {
		karaokeError(c, err)
		return nil, false
	}
	if user != nil {
		c.Set("karaoke_user_id", user.ID)
	}
	return user, true
}
func (a *KaraokeAccounts) captcha(c *gin.Context) {
	id, err := a.service.Captcha(c.Request.Context())
	if err != nil {
		karaokeError(c, err)
		return
	}
	c.JSON(200, gin.H{"challenge": id, "image_url": "/api/v1/karaoke/account/captcha/" + id})
}
func (a *KaraokeAccounts) captchaImage(c *gin.Context) {
	svg, err := a.service.CaptchaSVG(c.Request.Context(), c.Param("challenge"))
	if err != nil {
		karaokeError(c, err)
		return
	}
	c.Header("X-Content-Type-Options", "nosniff")
	c.Data(200, "image/svg+xml", []byte(svg))
}
func (a *KaraokeAccounts) auth(c *gin.Context, register bool) {
	if err := a.service.RequireMaster(c.Request.Context()); err != nil {
		karaokeError(c, err)
		return
	}
	var body struct {
		Username  string   `json:"username"`
		Password  string   `json:"password"`
		Challenge string   `json:"challenge"`
		Captcha   string   `json:"captcha"`
		Addresses []string `json:"webrtc_addresses"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	if n := utf8.RuneCountInString(body.Username); n < 1 || n > 64 || utf8.RuneCountInString(body.Password) < 1 || utf8.RuneCountInString(body.Password) > 128 || len(body.Challenge) > 32 || utf8.RuneCountInString(body.Captcha) > 12 || len(body.Addresses) > 8 {
		invalid(c, "body", "credentials")
		return
	}
	info := a.info(c)
	info.Addresses = body.Addresses
	var user store.KaraokeUser
	var login karaoke.Session
	var err error
	if register {
		user, login, err = a.service.Register(c.Request.Context(), body.Username, body.Password, body.Challenge, body.Captcha, info)
	} else {
		user, login, err = a.service.Login(c.Request.Context(), body.Username, body.Password, body.Challenge, body.Captcha, info)
	}
	if err != nil {
		karaokeError(c, err)
		return
	}
	a.cookies(c, login, false)
	c.JSON(200, gin.H{"status": "ok", "user": userJSON(user)})
}
func (a *KaraokeAccounts) status(c *gin.Context) {
	role, err := a.public.media.BusinessRole(c.Request.Context())
	if err != nil {
		internalError(c, err)
		return
	}
	if role != "Master" {
		c.JSON(200, gin.H{"available": false, "authenticated": false, "reason": "请在 Master 节点使用账号与录音"})
		return
	}
	user, ok := a.current(c, false, true)
	if !ok {
		return
	}
	pool, err := a.public.media.StoragePool(c.Request.Context())
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	storage := false
	if members, ok := pool["members"].([]store.StorageMember); ok {
		for _, m := range members {
			if m.ID != "auto" && m.Enabled != 0 && m.Writable != 0 && m.Health == "online" {
				storage = true
				break
			}
		}
	}
	var result any
	if user != nil {
		result = userJSON(*user)
	}
	c.JSON(200, gin.H{"available": true, "authenticated": user != nil, "user": result, "storage_available": storage})
}
func (a *KaraokeAccounts) logout(c *gin.Context) {
	user, ok := a.current(c, true, false)
	if !ok {
		return
	}
	name, _ := a.cookieNames()
	token, _ := c.Cookie(name)
	if err := a.service.Logout(c.Request.Context(), *user, token, a.info(c)); err != nil {
		karaokeError(c, err)
		return
	}
	a.cookies(c, karaoke.Session{}, true)
	c.JSON(200, gin.H{"status": "ok"})
}
func (a *KaraokeAccounts) password(c *gin.Context) {
	user, ok := a.current(c, true, false)
	if !ok {
		return
	}
	var body struct {
		Current string `json:"current_password"`
		Next    string `json:"new_password"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	if utf8.RuneCountInString(body.Current) < 1 || utf8.RuneCountInString(body.Current) > 128 || utf8.RuneCountInString(body.Next) < 1 || utf8.RuneCountInString(body.Next) > 128 {
		invalid(c, "body", "password")
		return
	}
	if err := a.service.Password(c.Request.Context(), *user, body.Current, body.Next, a.info(c)); err != nil {
		karaokeError(c, err)
		return
	}
	a.cookies(c, karaoke.Session{}, true)
	c.JSON(200, gin.H{"status": "ok", "relogin_required": true})
}
func (a *KaraokeAccounts) deleteAccount(c *gin.Context) {
	user, ok := a.current(c, true, false)
	if !ok {
		return
	}
	_, complete, err := a.service.DeleteUser(c.Request.Context(), user.ID, a.info(c), "")
	if err != nil {
		karaokeError(c, err)
		return
	}
	a.cookies(c, karaoke.Session{}, true)
	status := "deleting"
	if complete {
		status = "deleted"
	}
	c.JSON(200, gin.H{"status": status})
}
func (a *KaraokeAccounts) listUsers(c *gin.Context) {
	q := strings.TrimSpace(c.Query("q"))
	if utf8.RuneCountInString(q) == 1 {
		detail(c, 400, "用户查询至少输入 2 个字符")
		return
	}
	runes := []rune(q)
	if len(runes) > 64 {
		q = string(runes[:64])
	}
	page, size := 1, 50
	for key, target := range map[string]*int{"page": &page, "page_size": &size} {
		if raw, ok := c.GetQuery(key); ok {
			value, err := strconv.Atoi(raw)
			if err != nil {
				invalid(c, "query", key)
				return
			}
			*target = value
		}
	}
	result, err := a.service.ListUsers(c.Request.Context(), q, page, size)
	if err != nil {
		karaokeError(c, err)
		return
	}
	items := []gin.H{}
	for _, v := range result.Items {
		entry := userJSON(v)
		entry["created_at"] = v.CreatedAt
		items = append(items, entry)
	}
	c.JSON(200, gin.H{"items": items, "pagination": gin.H{"page": result.Page, "pages": result.Pages, "total": result.Total}})
}
func (a *KaraokeAccounts) mutateUser(c *gin.Context) {
	var body struct {
		Action string `json:"action"`
		Quota  *int64 `json:"quota_mib"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	if body.Action != "ban" && body.Action != "unban" && body.Action != "quota" && body.Action != "delete" {
		invalid(c, "body", "action")
		return
	}
	if body.Quota != nil && (*body.Quota < 1 || *body.Quota > 10*1024*1024) || body.Action == "quota" && body.Quota == nil {
		invalid(c, "body", "quota_mib")
		return
	}
	id, actor := c.Param("user"), session(c).Hash
	if body.Action == "delete" {
		found, complete, err := a.service.DeleteUser(c.Request.Context(), id, a.info(c), actor)
		if err != nil {
			karaokeError(c, err)
			return
		}
		if !found {
			detail(c, 404, "用户不存在")
			return
		}
		status := "deleting"
		if complete {
			status = "deleted"
		}
		c.JSON(200, gin.H{"status": status})
		return
	}
	quota := int64(0)
	if body.Quota != nil {
		quota = *body.Quota * 1024 * 1024
	}
	if err := a.service.MutateUser(c.Request.Context(), id, body.Action, quota, a.info(c), actor); err != nil {
		karaokeError(c, err)
		return
	}
	c.JSON(200, gin.H{"status": "ok"})
}
