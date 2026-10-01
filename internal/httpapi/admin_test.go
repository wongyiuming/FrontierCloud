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
	"github.com/wongyiuming/FrontierCloud/internal/node"
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
	if _, err := RegisterAdmin(router, cfg, auth, public, identity); err != nil {
		t.Fatal(err)
	}
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
