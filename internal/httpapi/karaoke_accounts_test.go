package httpapi

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/admin"
	"github.com/wongyiuming/FrontierCloud/internal/karaoke"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func karaokeSessionHash(value string) string {
	h := sha256.Sum256([]byte(value))
	return hex.EncodeToString(h[:])
}

func TestKaraokeAccountHTTPRealRedisCaptchaSessionCSRFRevocationAndAdmin(t *testing.T) {
	raw := os.Getenv("FRONTIERCLOUD_TEST_REDIS_URL")
	if raw == "" {
		t.Skip("disposable Redis not configured")
	}
	opts, err := redis.ParseURL(raw)
	if err != nil {
		t.Fatal(err)
	}
	cache := redis.NewClient(opts)
	defer cache.Close()
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	router, db, dir, public, master := clusterFixture(t, "https://master.test", transport, false)
	if _, err := master.Promote(ctx, "Master", "https://master.test", 10*store.GiB, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	cfg := public.settings
	cfg.SecretsDirectory = filepath.Join(dir, "secrets")
	const key = "native-accounts-admin-key"
	if err := os.WriteFile(filepath.Join(cfg.SecretsDirectory, "admin_key"), []byte(key), 0600); err != nil {
		t.Fatal(err)
	}
	identity, err := node.Initialize(ctx, db.Nodes(), cfg.SecretsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	auth, err := admin.New(cfg, admin.NewRedisCache(cache), db.Admin())
	if err != nil {
		t.Fatal(err)
	}
	adminHTTP, err := RegisterAdmin(router, cfg, auth, public, identity)
	if err != nil {
		t.Fatal(err)
	}
	resolver, _ := network.New(nil)
	service := karaoke.New(db.Karaoke(), db.Nodes(), karaoke.NewRedisCache(cache))
	RegisterKaraokeAccounts(router, service, public, adminHTTP, resolver)
	cookies := map[string]*http.Cookie{}
	perform := func(method, target, body, kind string, csrf bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "https://master.test"+target, strings.NewReader(body))
		r.RemoteAddr = "192.0.2.178:9876"
		if kind != "" {
			r.Header.Set("Content-Type", kind)
		}
		for _, cookie := range cookies {
			r.AddCookie(cookie)
		}
		if csrf {
			if c := cookies["__Host-karaoke_csrf"]; c != nil {
				r.Header.Set("X-Karaoke-CSRF", c.Value)
			}
			if c := cookies[cfg.CSRFCookieName()]; c != nil {
				r.Header.Set("X-CSRF-Token", c.Value)
			}
		}
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		for _, c := range w.Result().Cookies() {
			if c.MaxAge < 0 {
				delete(cookies, c.Name)
			} else {
				cookies[c.Name] = c
			}
		}
		return w
	}
	challenge := func() (string, string) {
		w := perform("GET", "/api/v1/karaoke/account/captcha", "", "", false)
		var result struct {
			Challenge string `json:"challenge"`
			URL       string `json:"image_url"`
		}
		if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &result) != nil {
			t.Fatal("captcha", w.Code, w.Body.String())
		}
		image := perform("GET", result.URL, "", "", false)
		if image.Code != 200 || !strings.Contains(image.Body.String(), "<svg") || image.Header().Get("X-Content-Type-Options") != "nosniff" {
			t.Fatal("SVG", image.Code, image.Body.String())
		}
		answer, err := cache.Get(ctx, "karaoke:captcha:"+result.Challenge+":image").Result()
		if err != nil {
			t.Fatal(err)
		}
		return result.Challenge, answer
	}
	credential := func(username, password, id, answer string) string {
		body, _ := json.Marshal(gin.H{"username": username, "password": password, "challenge": id, "captcha": answer, "webrtc_addresses": []string{"192.0.2.178", "10.0.0.1"}})
		return string(body)
	}
	w := perform("GET", "/api/v1/karaoke/account/status", "", "", false)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"available":true`) || !strings.Contains(w.Body.String(), `"authenticated":false`) {
		t.Fatal(w.Code, w.Body.String())
	}
	id, answer := challenge()
	w = perform("POST", "/api/v1/karaoke/account/register", credential("Ｎａｔｉｖｅ账号", "Huawei@123", id, answer), "application/json", false)
	var created struct {
		User store.KaraokeUser `json:"user"`
	}
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &created) != nil || created.User.Username != "Native账号" || strings.Contains(w.Body.String(), "scrypt") || cookies["__Host-karaoke_session"] == nil || !cookies["__Host-karaoke_session"].HttpOnly || cookies["__Host-karaoke_csrf"].HttpOnly {
		t.Fatal("register/session", w.Code, w.Body.String())
	}
	userid := created.User.ID
	oldToken := cookies["__Host-karaoke_session"].Value
	sessionKey := "karaoke:session:" + karaokeSessionHash(oldToken)
	if ttl, err := cache.TTL(ctx, sessionKey).Result(); err != nil || ttl.Seconds() < float64(karaoke.SessionTTL-10) {
		t.Fatal("session TTL", ttl, err)
	}
	w = perform("GET", "/api/v1/karaoke/account/captcha/"+id, "", "", false)
	if w.Code != 404 {
		t.Fatal("captcha replay image", w.Code)
	}
	w = perform("POST", "/api/v1/karaoke/account/register", credential("Other账号", "Huawei@123", id, answer), "application/json", false)
	if w.Code != 400 {
		t.Fatal("captcha replay", w.Code, w.Body.String())
	}
	w = perform("POST", "/api/v1/karaoke/account/password", `{"current_password":"Huawei@123","new_password":"Changed@456"}`, "application/json", false)
	if w.Code != 403 {
		t.Fatal("CSRF bypass", w.Code, w.Body.String())
	}
	for i := range 3 {
		w = perform("POST", "/api/v1/karaoke/account/login", credential("native账号", "wrong", "", ""), "application/json", false)
		if w.Code != 401 || i == 2 && w.Header().Get("X-Captcha-Required") != "1" {
			t.Fatal("login failure", i, w.Code, w.Body.String())
		}
	}
	w = perform("POST", "/api/v1/karaoke/account/login", credential("native账号", "Huawei@123", "", ""), "application/json", false)
	if w.Code != 400 || w.Header().Get("X-Captcha-Required") != "1" {
		t.Fatal("fourth attempt bypass", w.Code, w.Body.String())
	}
	id, answer = challenge()
	w = perform("POST", "/api/v1/karaoke/account/login", credential("native账号", "Huawei@123", id, answer), "application/json", false)
	if w.Code != 200 {
		t.Fatal("captcha login", w.Code, w.Body.String())
	}
	w = perform("POST", "/api/v1/media/admin/elevate", "token="+url.QueryEscape(key), "application/x-www-form-urlencoded", false)
	if w.Code != 200 {
		t.Fatal("Admin login", w.Code, w.Body.String())
	}
	w = perform("POST", "/api/v1/media/admin/users/"+userid, `{"action":"ban"}`, "application/json", true)
	if w.Code != 200 {
		t.Fatal("ban", w.Code, w.Body.String())
	}
	w = perform("POST", "/api/v1/media/admin/users/"+userid, `{"action":"unban"}`, "application/json", true)
	if w.Code != 200 {
		t.Fatal("unban", w.Code, w.Body.String())
	}
	w = perform("GET", "/api/v1/karaoke/account/status", "", "", false)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"authenticated":false`) {
		t.Fatal("old session revived after unban", w.Code, w.Body.String())
	}
	w = perform("POST", "/api/v1/karaoke/account/login", credential("native账号", "Huawei@123", "", ""), "application/json", false)
	if w.Code != 200 {
		t.Fatal("login after unban", w.Code, w.Body.String())
	}
	beforeChange := cookies["__Host-karaoke_session"]
	w = perform("POST", "/api/v1/karaoke/account/password", `{"current_password":"Huawei@123","new_password":"weak"}`, "application/json", true)
	if w.Code != 400 {
		t.Fatal("password policy", w.Code, w.Body.String())
	}
	w = perform("POST", "/api/v1/karaoke/account/password", `{"current_password":"Huawei@123","new_password":"Changed@456"}`, "application/json", true)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"relogin_required":true`) {
		t.Fatal("password change", w.Code, w.Body.String())
	}
	cookies[beforeChange.Name] = beforeChange
	w = perform("GET", "/api/v1/karaoke/account/status", "", "", false)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"authenticated":false`) {
		t.Fatal("old password session survived", w.Code, w.Body.String())
	}
	w = perform("POST", "/api/v1/karaoke/account/login", credential("native账号", "Changed@456", "", ""), "application/json", false)
	if w.Code != 200 {
		t.Fatal("new password", w.Code, w.Body.String())
	}
	w = perform("GET", "/api/v1/media/admin/users?q=Native", "", "", false)
	if w.Code != 200 || !strings.Contains(w.Body.String(), userid) || strings.Contains(w.Body.String(), "password_hash") {
		t.Fatal("user search", w.Code, w.Body.String())
	}
	if _, err := db.Database().Exec("UPDATE karaoke_users SET used_bytes=? WHERE user_id=?", 150*1024*1024, userid); err != nil {
		t.Fatal(err)
	}
	w = perform("POST", "/api/v1/media/admin/users/"+userid, `{"action":"quota","quota_mib":100}`, "application/json", true)
	if w.Code != 409 {
		t.Fatal("quota below usage", w.Code, w.Body.String())
	}
	w = perform("POST", "/api/v1/media/admin/users/"+userid, `{"action":"quota","quota_mib":300}`, "application/json", true)
	if w.Code != 200 {
		t.Fatal("quota update", w.Code, w.Body.String())
	}
	if _, err := db.Database().Exec("UPDATE karaoke_users SET used_bytes=0 WHERE user_id=?", userid); err != nil {
		t.Fatal(err)
	}
	w = perform("DELETE", "/api/v1/karaoke/account", "", "", true)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"status":"deleted"`) {
		t.Fatal("account deletion", w.Code, w.Body.String())
	}
	user, err := db.Karaoke().UserByID(ctx, userid)
	if err != nil || user != nil {
		t.Fatal("deleted account retained", user, err)
	}
	// Legacy sessions intentionally require re-login, rather than reviving after
	// an unobserved password change without a native fingerprint/generation.
	if err := cache.HSet(ctx, sessionKey, "user_id", userid, "csrf", "old").Err(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cache.Del(context.Background(), sessionKey, "karaoke:user-generation:"+userid) })
	id, answer = challenge()
	w = perform("POST", "/api/v1/karaoke/account/register", credential("Native账号", "Huawei@123", id, answer), "application/json", false)
	if w.Code != 200 {
		t.Fatal("deleted username not reusable", w.Code, w.Body.String())
	}
	for range 20 {
		w = perform("POST", "/api/v1/karaoke/account/register", credential("Failure账号", "Huawei@123", "", ""), "application/json", false)
		if w.Code != 400 && w.Code != 429 {
			t.Fatal("registration failure", w.Code, w.Body.String())
		}
	}
	w = perform("POST", "/api/v1/karaoke/account/register", credential("Failure账号", "Huawei@123", "", ""), "application/json", false)
	if w.Code != 429 {
		t.Fatal("durable failure limit", w.Code, w.Body.String())
	}
}
