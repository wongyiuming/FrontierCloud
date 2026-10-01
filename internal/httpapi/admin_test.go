package httpapi

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/admin"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/observation"
	"github.com/wongyiuming/FrontierCloud/internal/security"
)

func TestAdminHTTPRedisContract(t *testing.T) {
	redisURL := os.Getenv("FRONTIERCLOUD_TEST_REDIS_URL")
	if redisURL == "" {
		t.Skip("disposable Redis integration URL not configured")
	}
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		t.Fatal(err)
	}
	client := redis.NewClient(opts)
	defer client.Close()
	router, db, dir, public := publicFixture(t, true)
	cfg := public.settings
	cfg.SecretsDirectory = filepath.Join(dir, "secrets")
	key := "http-test-persistent-key-not-a-secret"
	if err := os.WriteFile(filepath.Join(cfg.SecretsDirectory, "admin_key"), []byte(key), 0600); err != nil {
		t.Fatal(err)
	}
	identity, err := node.Initialize(context.Background(), db.Nodes(), cfg.SecretsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	auth, err := admin.New(cfg, admin.NewRedisCache(client), db.Admin())
	if err != nil {
		t.Fatal(err)
	}
	adminHTTP, err := RegisterAdmin(router, cfg, auth, public, identity)
	if err != nil {
		t.Fatal(err)
	}
	securityService, err := security.New(cfg, db.Security())
	if err != nil {
		t.Fatal(err)
	}
	defer securityService.Close()
	if err := securityService.Publish(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	RegisterSecurityAdmin(router, adminHTTP, securityService)
	resolver, _ := network.New(cfg.TrustedProxyNetworks)
	RegisterObservations(router, adminHTTP, observation.New(db.Observations(), client, 30), resolver)
	cookies := map[string]*http.Cookie{}
	perform := func(method, target, body, contentType, activity string, csrf bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "https://example.com"+target, strings.NewReader(body))
		r.TLS = &tls.ConnectionState{}
		if contentType != "" {
			r.Header.Set("Content-Type", contentType)
		}
		for _, cookie := range cookies {
			r.AddCookie(cookie)
		}
		if csrf && cookies[cfg.CSRFCookieName()] != nil {
			r.Header.Set("X-CSRF-Token", cookies[cfg.CSRFCookieName()].Value)
		}
		if activity != "" {
			r.Header.Set("X-Admin-Activity", activity)
		}
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		for _, cookie := range w.Result().Cookies() {
			if cookie.MaxAge < 0 {
				delete(cookies, cookie.Name)
			} else {
				cookies[cookie.Name] = cookie
			}
		}
		return w
	}
	bad := httptest.NewRequest("POST", "http://example.com/api/v1/media/admin/elevate", strings.NewReader("token="+key))
	bad.Header.Set("X-Forwarded-Proto", "https")
	bad.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	bad.RemoteAddr = "192.0.2.1:123"
	w := httptest.NewRecorder()
	router.ServeHTTP(w, bad)
	if w.Code != 426 {
		t.Fatalf("spoofed transport accepted: %d", w.Code)
	}
	w = perform("GET", "/api/v1/media/admin/status", "", "", "", false)
	if w.Code != 401 {
		t.Fatalf("anonymous status %d", w.Code)
	}
	w = perform("POST", "/api/v1/media/admin/elevate", "token="+url.QueryEscape(key), "application/x-www-form-urlencoded", "", false)
	if w.Code != 200 || len(cookies) != 2 || !cookies[cfg.AdminCookieName()].HttpOnly || cookies[cfg.CSRFCookieName()].HttpOnly {
		t.Fatalf("login contract: %d %s %v", w.Code, w.Body.String(), w.Header())
	}
	w = perform("GET", "/api/v1/media/admin/status", "", "", "passive", false)
	if w.Code != 200 || len(w.Header().Values("Set-Cookie")) != 0 || !strings.Contains(w.Body.String(), `"node_role":"Standalone"`) {
		t.Fatalf("passive status: %d %s", w.Code, w.Body.String())
	}
	w = perform("GET", "/api/v1/media/admin", "", "", "", false)
	if w.Code != 200 || strings.Contains(w.Body.String(), "{{") || !strings.Contains(w.Body.String(), "admin-visibility-integrity.js?v=") {
		t.Fatalf("admin shell: %d %.100s", w.Code, w.Body.String())
	}
	for _, test := range []struct {
		method, target, body, want string
		status                     int
	}{
		{"GET", "/api/v1/media/admin/tree?path=music/nested", "", `"path":"music/nested/album"`, 200},
		{"POST", "/api/v1/media/admin/hide", `{"paths":["music/nested"],"hidden":null}`, "", 400},
		{"POST", "/api/v1/media/admin/hide", `{"paths":["music/nested"],"hidden":true}`, `"hidden":true`, 200},
		{"GET", "/api/v1/media/admin/tree?path=music/nested/album", "", `"hidden_direct":false`, 200},
		{"POST", "/api/v1/media/admin/hide", `{"paths":["music/nested"],"hidden":false}`, `"hidden":false`, 200},
		{"POST", "/api/v1/media/admin/directory-priority", `{"path":"music/nested","value":500}`, `"directory_path":"music/nested"`, 200},
		{"GET", "/api/v1/media/admin/directory-priorities?scope=music", "", `"preference":500`, 200},
		{"POST", "/api/v1/media/admin/media-priority", `{"media_path":"music/artist/song.mp3","value":7}`, `"preference":7`, 200},
		{"GET", "/api/v1/media/admin/media-priority?path=music/artist", "", `"preference":7`, 200},
		{"GET", "/api/v1/media/admin/tree/search?path=music&q=song", "", `"name":"song.mp3"`, 200},
		{"GET", "/api/v1/media/admin/tree/search?q=song", "", "", 400},
		{"GET", "/api/v1/media/admin/tree/search?path=music&q=%23", "", "", 400},
		{"GET", "/api/v1/media/admin/download?paths=%5B%22music%2Fartist%2Fsong.mp3%22%5D", "", "", 200},
		{"GET", "/api/v1/media/admin/download?paths=%5B%22music%2Fnested%22%5D", "", "PK", 200},
		{"POST", "/api/v1/media/admin/delete", `{"paths":["music/artist/song.mp3"]}`, `"deleted":1`, 200},
		{"POST", "/api/v1/media/admin/delete", `{"paths":["lyrics/default.lrc"]}`, "", 400},
	} {
		w = perform(test.method, test.target, test.body, "application/json", "", true)
		if w.Code != test.status || test.want != "" && !strings.Contains(w.Body.String(), test.want) {
			t.Fatalf("%s %s: %d %s", test.method, test.target, w.Code, w.Body.String())
		}
	}
	for _, test := range []struct {
		filename, payload, relative, path string
		lyric                             bool
		status                            int
	}{
		{"暗湧.mp3", "ID3native-upload", "", "music/artist/暗涌.mp3", false, 200},
		{"暗湧.mp3", "ID3replacement", "", "", false, 409},
		{"bad.mp3", "invalid-media", "", "", false, 400},
		{"song.lrc", "[00:01]暗涌\n", "artist/album/暗湧.lrc", "lyrics/artist/album/暗涌.lrc", true, 200},
	} {
		var body bytes.Buffer
		writer := multipart.NewWriter(&body)
		file, err := writer.CreateFormFile("file", test.filename)
		if err != nil {
			t.Fatal(err)
		}
		file.Write([]byte(test.payload))
		writer.WriteField("target_dir", "music/artist")
		if test.relative != "" {
			writer.WriteField("relative_path", test.relative)
		}
		writer.Close()
		target := "/api/v1/media/admin/upload/item"
		if test.lyric {
			target = "/api/v1/media/admin/upload/lyric"
		}
		w = perform("POST", target, body.String(), writer.FormDataContentType(), "", true)
		if w.Code != test.status || test.path != "" && !strings.Contains(w.Body.String(), test.path) {
			t.Fatalf("upload %s: %d %s", test.filename, w.Code, w.Body.String())
		}
	}
	reader := &countedBody{reader: strings.NewReader(strings.Repeat("large-body", 10000))}
	client.Del(context.Background(), "webrtc:observation:192.0.2.1")
	for _, test := range []struct {
		method, target, body, want string
		status                     int
	}{
		{"POST", "/api/v1/media/network-observation", `{"addresses":["0.0.0.0"]}`, "", 400},
		{"POST", "/api/v1/media/network-observation", `{"addresses":["192.0.2.1","2001:db8::1"]}`, `"matches_verified":true`, 200},
		{"POST", "/api/v1/media/network-observation", `{"failure":"timeout"}`, "", 429},
		{"GET", "/api/v1/media/admin/network/observations?public_ip=192.0.2.1", "", `"total":2`, 200},
		{"GET", "/api/v1/media/admin/network/observations?view=public&public_ip=192.0.2.1", "", `"relation_count":2`, 200},
		{"GET", "/api/v1/media/admin/network/observations?view=webrtc&webrtc_ip=2001:db8::1", "", `"key":"2001:db8::1"`, 200},
	} {
		w = perform(test.method, test.target, test.body, "application/json", "", true)
		if w.Code != test.status || test.want != "" && !strings.Contains(w.Body.String(), test.want) {
			t.Fatalf("network observation %s: %d %s", test.target, w.Code, w.Body.String())
		}
	}
	for _, test := range []struct {
		method, target, body, want string
		status                     int
	}{
		{"POST", "/api/v1/media/admin/security/reban", `{"ip":"198.51.100.100","reason":"test ban"}`, `"expires_at"`, 200},
		{"GET", "/api/v1/media/admin/security/blocks?ip=198.51.100.100", "", `"status":"active"`, 200},
		{"POST", "/api/v1/media/admin/security/permanent-ban", `{"ip":"198.51.100.100","reason":"test permanent"}`, `"permanent":true`, 200},
		{"POST", "/api/v1/media/admin/security/whitelist", `{"ip":"198.51.100.100","note":"test allow"}`, `"status":"ok"`, 200},
		{"GET", "/api/v1/media/admin/security/blocks?ip=198.51.100.100", "", `"note":"test allow"`, 200},
		{"POST", "/api/v1/media/admin/security/whitelist/remove", `{"ip":"198.51.100.100"}`, `"status":"ok"`, 200},
		{"POST", "/api/v1/media/admin/security/unban", `{"ip":"198.51.100.100"}`, `"status":"ok"`, 200},
		{"GET", "/api/v1/media/admin/security/blocks?match_mode=fuzzy&ip=19", "", "", 400},
		{"POST", "/api/v1/media/admin/security/reban", `{"ip":"127.0.0.1","reason":"loopback"}`, "", 400},
	} {
		w = perform(test.method, test.target, test.body, "application/json", "", true)
		if w.Code != test.status || test.want != "" && !strings.Contains(w.Body.String(), test.want) {
			t.Fatalf("security %s: %d %s", test.target, w.Code, w.Body.String())
		}
	}
	for _, test := range []struct {
		method, target, body, want string
		status                     int
	}{
		{"GET", "/api/v1/media/admin/lyrics/catalog?track_path=music&lyric_path=lyrics", "", `"track_directories"`, 200},
		{"POST", "/api/v1/media/admin/lyrics/relations", `{"origin_kind":"track","origin_path":"music/artist/暗涌.mp3","linked_paths":["lyrics/artist/album/暗涌.lrc"]}`, `"relations":1`, 200},
		{"GET", "/api/v1/media/admin/lyrics/catalog?track_path=music&lyric_path=lyrics&track_q=anyong", "", `"lyric_path":"lyrics/artist/album/暗涌.lrc"`, 200},
		{"POST", "/api/v1/media/admin/lyrics/auto-relate", `{"manual":false}`, "", 400},
		{"POST", "/api/v1/media/admin/lyrics/auto-relate", `{"manual":true}`, `"preserved":1`, 200},
		{"POST", "/api/v1/media/admin/directory/rename", `{"path":"music/artist","new_name":"renamed"}`, `"new_path":"music/renamed"`, 200},
		{"GET", "/api/v1/media/admin/lyrics/catalog?track_path=music/renamed&lyric_path=lyrics", "", `"path":"music/renamed/暗涌.mp3"`, 200},
	} {
		w = perform(test.method, test.target, test.body, "application/json", "", true)
		if w.Code != test.status || test.want != "" && !strings.Contains(w.Body.String(), test.want) {
			t.Fatalf("lyric/directory %s: %d %s", test.target, w.Code, w.Body.String())
		}
	}
	unauthorizedUpload := httptest.NewRequest("POST", "https://example.com/api/v1/media/admin/upload/item", reader)
	unauthorizedUpload.TLS = &tls.ConnectionState{}
	unauthorizedUpload.Header.Set("Content-Type", "multipart/form-data; boundary=test")
	for _, cookie := range cookies {
		unauthorizedUpload.AddCookie(cookie)
	}
	denied := httptest.NewRecorder()
	router.ServeHTTP(denied, unauthorizedUpload)
	if denied.Code != 403 || reader.read != 0 {
		t.Fatalf("upload read before CSRF verification: status %d, bytes %d", denied.Code, reader.read)
	}
	w = perform("POST", "/api/v1/media/admin/key/temporary", `{"minutes":15}`, "application/json", "", false)
	if w.Code != 403 {
		t.Fatalf("CSRF bypass: %d", w.Code)
	}
	w = perform("POST", "/api/v1/media/admin/key/temporary", `{"minutes":15}`, "application/json", "", true)
	if w.Code != 200 {
		t.Fatalf("temporary key: %s", w.Body.String())
	}
	var temporary struct {
		Key string `json:"admin_key"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &temporary); err != nil || temporary.Key == "" {
		t.Fatal("missing temporary key")
	}
	w = perform("POST", "/api/v1/media/admin/key/rotate", `{"mode":"random"}`, "application/json", "", true)
	if w.Code != 200 || w.Header().Get("Cache-Control") != "private, no-store" {
		t.Fatalf("rotation: %s", w.Body.String())
	}
	w = perform("GET", "/api/v1/media/admin/status", "", "", "", false)
	if w.Code != 200 {
		t.Fatalf("initiator revoked: %s", w.Body.String())
	}
	w = perform("GET", "/api/v1/media/admin/brand", "", "", "", false)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"max_upload_bytes":8388608`) {
		t.Fatalf("brand status: %s", w.Body.String())
	}
	var multipartBody bytes.Buffer
	writer := multipart.NewWriter(&multipartBody)
	file, err := writer.CreateFormFile("file", "music.png")
	if err != nil {
		t.Fatal(err)
	}
	file.Write([]byte{137, 80, 78, 71, 13, 10, 26, 10, 1, 2})
	writer.Close()
	w = perform("POST", "/api/v1/media/admin/upload/brand/music", multipartBody.String(), writer.FormDataContentType(), "", true)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"custom":true`) {
		t.Fatalf("brand upload: %d %s", w.Code, w.Body.String())
	}
	w = perform("GET", "/api/v1/media/admin/brand/music/download", "", "", "", false)
	if w.Code != 200 || !strings.Contains(w.Header().Get("Content-Disposition"), "attachment") {
		t.Fatalf("brand download: %d", w.Code)
	}
	w = perform("DELETE", "/api/v1/media/admin/brand/music", "", "", "", true)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"custom":false`) {
		t.Fatalf("brand delete: %s", w.Body.String())
	}
	w = perform("POST", "/api/v1/media/admin/logout", "", "", "", true)
	if w.Code != 200 || len(cookies) != 0 {
		t.Fatalf("logout: %d %v", w.Code, cookies)
	}
	w = perform("POST", "/api/v1/media/admin/elevate", "token="+url.QueryEscape(temporary.Key), "application/x-www-form-urlencoded", "", false)
	if w.Code != 403 {
		t.Fatalf("temporary key survived rotation: %d", w.Code)
	}
	var auditCount int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log").Scan(&auditCount); err != nil || auditCount < 5 {
		t.Fatalf("audit evidence missing: %d %v", auditCount, err)
	}
}

type countedBody struct {
	reader *strings.Reader
	read   int
}

func (r *countedBody) Read(buffer []byte) (int, error) {
	n, err := r.reader.Read(buffer)
	r.read += n
	return n, err
}
