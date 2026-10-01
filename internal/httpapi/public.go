package httpapi

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"html"
	"log/slog"
	"mime"
	"net/http"
	"net/url"
	"os"
	"path"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/brand"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type Public struct {
	settings config.Config
	media    *media.Service
	static   *os.Root
	assets   map[string]string
	brand    *brand.Service
}

func assetURL(name string, content []byte) string {
	sum := sha256.Sum256(content)
	return "/static/" + name + "?v=" + hex.EncodeToString(sum[:8])
}

// RegisterPublic validates required frontend assets at startup; missing assets
// fail the deployment instead of leaving unresolved placeholders in HTML.
func RegisterPublic(router *gin.Engine, settings config.Config, service *media.Service) (*Public, error) {
	root, err := os.OpenRoot(settings.StaticRoot)
	if err != nil {
		return nil, err
	}
	p := &Public{settings: settings, media: service, static: root, assets: map[string]string{}}
	for _, name := range []string{"js/player.js", "css/player.css", "js/lyrics.js", "css/lyrics.css", "js/media-browser.js", "js/network-observation.js", "js/player-directory-label.js", "js/audio-continuous-stream.js", "css/karaoke.css", "js/karaoke.js"} {
		content, err := root.ReadFile(name)
		if err != nil {
			root.Close()
			return nil, err
		}
		sum := sha256.Sum256(content)
		p.assets[name] = "/static/" + name + "?v=" + hex.EncodeToString(sum[:8])
	}
	for _, name := range []string{"index.html", "category.html", "audio-player.html", "video-player.html", "lyrics.html", "karaoke.html"} {
		if _, err := root.ReadFile("media/" + name); err != nil {
			root.Close()
			return nil, err
		}
	}
	router.GET("/", func(c *gin.Context) { c.Redirect(http.StatusTemporaryRedirect, "/api/v1/media") })
	router.GET("/favicon.ico", func(c *gin.Context) { p.staticFile(c, "favicon.ico", true) })
	router.GET("/static/*asset", func(c *gin.Context) { p.staticFile(c, strings.TrimPrefix(c.Param("asset"), "/"), false) })
	router.HEAD("/static/*asset", func(c *gin.Context) { p.staticFile(c, strings.TrimPrefix(c.Param("asset"), "/"), false) })
	router.GET("/karaoke/", p.karaokePage)
	group := router.Group("/api/v1/media")
	group.GET("", func(c *gin.Context) { p.page(c, "index.html", nil) })
	group.GET("/", func(c *gin.Context) { p.page(c, "index.html", nil) })
	group.GET("/refresh", func(c *gin.Context) {
		id, err := randomUUID()
		if err != nil {
			internalError(c, err)
			return
		}
		noStore(c)
		c.Header("Clear-Site-Data", `"cache"`)
		c.Redirect(http.StatusSeeOther, "/api/v1/media?ui="+strings.ReplaceAll(id, "-", ""))
	})
	group.GET("/catalog/categories", p.categories)
	group.GET("/catalog/media", p.catalog)
	group.GET("/music", func(c *gin.Context) { p.categoryPage(c, "music") })
	group.GET("/video", func(c *gin.Context) { p.categoryPage(c, "video") })
	group.GET("/music/category", func(c *gin.Context) { p.playerPage(c, "music") })
	group.GET("/video/category", func(c *gin.Context) { p.playerPage(c, "video") })
	group.GET("/stream", p.stream)
	group.HEAD("/stream", p.stream)
	group.POST("/playback", p.playback)
	group.GET("/lyrics/content", p.lyricsContent)
	group.GET("/lyrics", p.lyricsPage)
	p.brand, err = brand.New(settings.DataRoot, settings.StaticRoot)
	if err != nil {
		root.Close()
		return nil, err
	}
	router.GET("/api/v1/media/brand/logo/:kind", p.brandLogo)
	return p, nil
}

func (p *Public) Close() error                          { return errors.Join(p.static.Close(), p.brand.Close()) }
func detail(c *gin.Context, status int, message string) { c.JSON(status, gin.H{"detail": message}) }
func internalError(c *gin.Context, err error) {
	if errors.Is(err, media.ErrRecovery) {
		detail(c, 503, "媒体事务等待恢复，请在数据库恢复后重启服务")
		return
	}
	slog.Error("request failed", "path", c.Request.URL.Path, "error", err)
	detail(c, 500, "Internal server error")
}
func invalid(c *gin.Context, location, field string) {
	c.JSON(422, gin.H{"detail": []gin.H{{"type": "value_error", "loc": []string{location, field}, "msg": "Invalid value"}}})
}
func noStore(c *gin.Context) {
	c.Header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
	c.Header("Pragma", "no-cache")
	c.Header("Expires", "0")
}
func jsonString(value any) string {
	raw, _ := json.Marshal(value)
	return strings.ReplaceAll(string(raw), "'", `\u0027`)
}

func kindQuery(c *gin.Context) (string, bool) {
	kind := c.Query("media_type")
	if kind != "music" && kind != "video" {
		invalid(c, "query", "media_type")
		return "", false
	}
	return kind, true
}
func queryText(c *gin.Context, name string, max int) (string, bool) {
	value := c.Query(name)
	if value == "" || !utf8.ValidString(value) || utf8.RuneCountInString(value) > max {
		invalid(c, "query", name)
		return "", false
	}
	return value, true
}
func queryBool(c *gin.Context, name string) (bool, bool) {
	value, exists := c.GetQuery(name)
	if !exists {
		return false, true
	}
	switch strings.ToLower(value) {
	case "true", "1", "on", "yes", "t", "y":
		return true, true
	case "false", "0", "off", "no", "f", "n":
		return false, true
	}
	invalid(c, "query", name)
	return false, false
}

func (p *Public) categories(c *gin.Context) {
	kind, ok := kindQuery(c)
	if !ok {
		return
	}
	include, ok := queryBool(c, "include_hidden")
	if !ok {
		return
	}
	entries, err := p.media.Categories(c.Request.Context(), kind, include)
	if err != nil {
		internalError(c, err)
		return
	}
	c.Header("Cache-Control", "private, no-cache, must-revalidate")
	c.JSON(200, gin.H{"entries": entries})
}
func (p *Public) catalog(c *gin.Context) {
	kind, ok := kindQuery(c)
	if !ok {
		return
	}
	name, ok := queryText(c, "path", 1024)
	if !ok {
		return
	}
	session, ok := queryText(c, "playback_session_id", 64)
	if !ok {
		return
	}
	include, ok := queryBool(c, "include_hidden")
	if !ok {
		return
	}
	entries, err := p.media.Catalog(c.Request.Context(), kind, name, session, include)
	if err != nil {
		if errors.Is(err, media.ErrCategory) {
			detail(c, 404, err.Error())
		} else {
			internalError(c, err)
		}
		return
	}
	c.Header("Cache-Control", "private, no-cache, must-revalidate")
	c.JSON(200, gin.H{"entries": entries})
}

func (p *Public) stream(c *gin.Context) {
	name := c.Query("file_path")
	id := c.Query("resource_id")
	if name == "" && id == "" {
		detail(c, 422, "file_path or resource_id is required")
		return
	}
	delivery, err := p.media.Delivery(c.Request.Context(), name, id, c.GetString("request_id"), c.GetString("trace_id"), p.settings.NginxMedia)
	if err != nil {
		if errors.Is(err, media.ErrUnavailable) {
			noStore(c)
			c.Header("Retry-After", "30")
			detail(c, 503, err.Error())
		} else if errors.Is(err, media.ErrPath) {
			detail(c, 403, "Forbidden media path access")
		} else if errors.Is(err, os.ErrNotExist) {
			detail(c, 404, "Media file not found")
		} else {
			internalError(c, err)
		}
		return
	}
	c.Header("X-Media-Resource-ID", delivery.ResourceID)
	c.Header("X-Media-Owner-ID", delivery.OwnerID)
	c.Header("X-Media-Object-ID", delivery.ObjectID)
	if delivery.Redirect != "" {
		noStore(c)
		c.Header("Referrer-Policy", "no-referrer")
		c.Redirect(http.StatusTemporaryRedirect, delivery.Redirect)
		return
	}
	if delivery.Relay != "" {
		noStore(c)
		c.Header("X-Accel-Redirect", delivery.Relay)
		c.Status(200)
		return
	}
	stream := delivery.Stream
	defer stream.File.Close()
	c.Header("Cache-Control", "public, max-age=86400")
	contentType := mime.TypeByExtension(path.Ext(stream.Path))
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	c.Header("Content-Type", contentType)
	if p.settings.NginxMedia {
		c.Header("X-Accel-Redirect", "/_protected_media/"+quoteMediaPath(stream.Path))
		c.Status(200)
		return
	}
	http.ServeContent(c.Writer, c.Request, path.Base(stream.Path), stream.Info.ModTime(), stream.File)
}
func quoteMediaPath(name string) string {
	return media.QuotePath(name)
}

func (p *Public) playback(c *gin.Context) {
	var body struct {
		MediaPath  string  `json:"media_path"`
		ResourceID *string `json:"resource_id"`
		Session    string  `json:"playback_session_id"`
		Played     float64 `json:"played_seconds"`
		Duration   float64 `json:"duration"`
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 512*1024)
	if err := c.ShouldBindJSON(&body); err != nil {
		invalid(c, "body", "payload")
		return
	}
	if body.MediaPath == "" || utf8.RuneCountInString(body.MediaPath) > 1024 {
		invalid(c, "body", "media_path")
		return
	}
	if body.Session == "" || len(body.Session) > 64 {
		invalid(c, "body", "playback_session_id")
		return
	}
	if body.Played <= 0 || body.Played > 86400 || body.Duration <= 0 || body.Duration > 86400 {
		invalid(c, "body", "duration")
		return
	}
	var result store.PlaybackResult
	var err error
	if body.ResourceID != nil {
		result, err = p.media.GlobalPlayback(c.Request.Context(), body.MediaPath, *body.ResourceID, body.Session, body.Played, body.Duration)
	} else {
		result, err = p.media.Playback(c.Request.Context(), body.MediaPath, body.Session, body.Played, body.Duration)
	}
	if err != nil {
		if errors.Is(err, media.ErrPath) || errors.Is(err, os.ErrNotExist) || err.Error() == "Invalid playback session" || err.Error() == "Playback threshold not reached" {
			detail(c, 400, err.Error())
		} else if errors.Is(err, media.ErrUnavailable) {
			noStore(c)
			c.Header("Retry-After", "30")
			detail(c, 503, err.Error())
		} else {
			internalError(c, err)
		}
		return
	}
	c.JSON(200, result)
}

func (p *Public) lyrics(c *gin.Context) ([]media.LyricEntry, bool) {
	name, ok := queryText(c, "track", 1024)
	if !ok {
		return nil, false
	}
	entries, err := p.media.LyricsResource(c.Request.Context(), name, c.Query("resource_id"))
	if err != nil {
		if errors.Is(err, media.ErrLyrics) {
			detail(c, 404, "Lyrics not found")
		} else if errors.Is(err, os.ErrNotExist) {
			detail(c, 404, "Media object not found")
		} else if errors.Is(err, media.ErrUnavailable) {
			noStore(c)
			c.Header("Retry-After", "30")
			detail(c, 503, err.Error())
		} else {
			internalError(c, err)
		}
		return nil, false
	}
	return entries, true
}
func (p *Public) lyricsContent(c *gin.Context) {
	entries, ok := p.lyrics(c)
	if !ok {
		return
	}
	noStore(c)
	c.JSON(200, gin.H{"entries": entries})
}
func (p *Public) lyricsPage(c *gin.Context) {
	entries, ok := p.lyrics(c)
	if !ok {
		return
	}
	lines := []string{}
	for _, entry := range entries {
		lines = append(lines, entry.Text)
	}
	p.page(c, "lyrics.html", map[string]string{"LYRICS_JSON": jsonString(lines), "LINE_COUNT": strconv.Itoa(len(lines))})
}

func (p *Public) template(name string) (string, error) {
	bytes, err := p.static.ReadFile("media/" + name)
	if err != nil {
		return "", err
	}
	content := string(bytes)
	if name == "audio-player.html" || name == "video-player.html" {
		content = strings.Replace(content, "<span>四大发明</span>", `<span id="playerDirectoryLabel">当前目录</span>`, 1)
		if name == "audio-player.html" {
			content = strings.Replace(content, `<script src="/static/js/audio-continuous-stream.js"></script>`, `<script src="`+p.assets["js/audio-continuous-stream.js"]+`"></script>`, 1)
		}
		content = strings.Replace(content, "</body>", `<script src="`+p.assets["js/player-directory-label.js"]+`"></script>`+"\n</body>", 1)
	}
	return content, nil
}

func (p *Public) page(c *gin.Context, name string, extra map[string]string) {
	content, err := p.template(name)
	if err != nil {
		internalError(c, err)
		return
	}
	values := map[string]string{
		"STUN_URLS_JSON":             jsonString([]string{"stun:" + p.settings.ServerName + ":" + strconv.Itoa(p.settings.STUNPort)}),
		"WEBRTC_INTERVAL_MS":         strconv.Itoa(p.settings.WebRTCCooldown * 1000),
		"NETWORK_OBSERVATION_JS_URL": html.EscapeString(p.assets["js/network-observation.js"]),
		"PLAYER_JS_URL":              html.EscapeString(p.assets["js/player.js"]), "PLAYER_CSS_URL": html.EscapeString(p.assets["css/player.css"]),
		"LYRICS_JS_URL": html.EscapeString(p.assets["js/lyrics.js"]), "LYRICS_CSS_URL": html.EscapeString(p.assets["css/lyrics.css"]),
		"MEDIA_BROWSER_JS_URL":       html.EscapeString(p.assets["js/media-browser.js"]),
		"MEDIA_PREFETCH_ASSETS_JSON": jsonString([]string{p.assets["js/player.js"], p.assets["css/player.css"], p.assets["js/lyrics.js"], p.assets["css/lyrics.css"]}),
	}
	for key, value := range extra {
		values[key] = value
	}
	for key, value := range values {
		content = strings.ReplaceAll(content, "{{"+key+"}}", value)
	}
	noStore(c)
	c.Data(200, "text/html; charset=utf-8", []byte(content))
}

func (p *Public) categoryPage(c *gin.Context, kind string) {
	include, ok := queryBool(c, "include_hidden")
	if !ok {
		return
	}
	suffix, state := "", "public"
	if include {
		suffix = "&include_hidden=true"
		state = "all"
	}
	title, empty := "前沿音乐", "暂无音乐分类目录，请在 data/media/music 下创建分类文件夹"
	if kind == "video" {
		title = "前沿视讯"
		empty = "暂无视频分类目录，请在 data/media/vido 下创建分类文件夹"
	}
	p.page(c, "category.html", map[string]string{"PAGE_TITLE": title, "BACK_URL": "/api/v1/media", "CATALOG_CONFIG_JSON": jsonString(gin.H{"url": "/api/v1/media/catalog/categories?media_type=" + kind + suffix, "cacheKey": "categories:" + kind + ":" + state, "emptyText": empty})})
}

func (p *Public) playerPage(c *gin.Context, kind string) {
	name, ok := queryText(c, "path", 1024)
	if !ok {
		return
	}
	include, ok := queryBool(c, "include_hidden")
	if !ok {
		return
	}
	direct, ok := queryBool(c, "direct")
	if !ok {
		return
	}
	if err := p.media.CategoryExists(c.Request.Context(), name, kind); err != nil {
		if errors.Is(err, media.ErrCategory) {
			detail(c, 404, "Media category not found")
		} else {
			internalError(c, err)
		}
		return
	}
	parts := strings.Split(name, "/")
	state, suffix := "public", ""
	if include {
		state = "all"
		suffix = "?include_hidden=true"
	}
	title, template := "前沿音乐", "audio-player.html"
	if kind == "video" {
		title = "前沿视讯"
		template = "video-player.html"
	}
	title += " - " + strings.Join(parts[1:], "/")
	back := "/api/v1/media/" + kind + suffix
	if len(parts) == 2 && !direct {
		entries, err := p.media.Subcategories(c.Request.Context(), kind, name, include)
		if err != nil {
			internalError(c, err)
			return
		}
		if len(entries) > 0 {
			p.page(c, "category.html", map[string]string{"PAGE_TITLE": html.EscapeString(title), "BACK_URL": html.EscapeString(back), "CATALOG_CONFIG_JSON": jsonString(gin.H{"bootstrap": entries, "cacheKey": "", "url": "", "emptyText": "暂无分类目录"})})
			return
		}
	}
	if len(parts) == 3 {
		values := url.Values{"path": {strings.Join(parts[:2], "/")}}
		if include {
			values.Set("include_hidden", "true")
		}
		back = "/api/v1/media/" + kind + "/category?" + values.Encode()
	}
	session, err := randomUUID()
	if err != nil {
		internalError(c, err)
		return
	}
	params := url.Values{"media_type": {kind}, "path": {name}, "playback_session_id": {session}}
	if include {
		params.Set("include_hidden", "true")
	}
	p.page(c, template, map[string]string{"PAGE_TITLE": html.EscapeString(title), "CATEGORY_LIST_URL": html.EscapeString(back), "MEDIA_JSON": "[]", "PLAYBACK_SESSION_ID": jsonString(session), "PLAYER_CATALOG_CONFIG_JSON": jsonString(gin.H{"url": "/api/v1/media/catalog/media?" + params.Encode(), "cacheKey": "media:" + kind + ":" + name + ":" + state})})
}

func randomUUID() (string, error) {
	bytes := make([]byte, 16)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	bytes[6] = (bytes[6] & 15) | 64
	bytes[8] = (bytes[8] & 63) | 128
	raw := hex.EncodeToString(bytes)
	return raw[:8] + "-" + raw[8:12] + "-" + raw[12:16] + "-" + raw[16:20] + "-" + raw[20:], nil
}

func (p *Public) staticFile(c *gin.Context, name string, favicon bool) {
	if name == "" || name != path.Clean(name) || strings.ContainsAny(name, "\\\x00") {
		detail(c, 404, "Not found")
		return
	}
	for _, part := range strings.Split(name, "/") {
		if strings.HasPrefix(part, ".") {
			detail(c, 404, "Not found")
			return
		}
	}
	f, err := p.static.Open(name)
	if err != nil {
		if favicon && errors.Is(err, os.ErrNotExist) {
			c.Status(204)
		} else {
			detail(c, 404, "Not found")
		}
		return
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		detail(c, 404, "Not found")
		return
	}
	http.ServeContent(c.Writer, c.Request, path.Base(name), info.ModTime(), f)
}

func (p *Public) karaokePage(c *gin.Context) {
	content, err := p.template("karaoke.html")
	if err != nil {
		internalError(c, err)
		return
	}
	values := map[string]string{"KARAOKE_CSS_URL": p.assets["css/karaoke.css"], "KARAOKE_JS_URL": p.assets["js/karaoke.js"], "NETWORK_OBSERVATION_JS_URL": p.assets["js/network-observation.js"], "STUN_URLS": jsonString([]string{"stun:" + p.settings.ServerName + ":" + strconv.Itoa(p.settings.STUNPort)}), "WEBRTC_INTERVAL_MS": strconv.Itoa(p.settings.WebRTCCooldown * 1000)}
	for key, value := range values {
		content = strings.ReplaceAll(content, "{{"+key+"}}", value)
	}
	c.Header("Cache-Control", "no-cache")
	c.Data(200, "text/html; charset=utf-8", []byte(content))
}
