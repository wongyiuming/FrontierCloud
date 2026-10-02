package httpapi

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/admin"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/release"
	"github.com/wongyiuming/FrontierCloud/internal/sitecontrol"
	storeSQLite "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNodeAdminRedisPromotionPairConfigurationRevocationAndReinitialize(t *testing.T) {
	redisURL := os.Getenv("FRONTIERCLOUD_TEST_REDIS_URL")
	if redisURL == "" {
		t.Skip("requires isolated Redis")
	}
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		t.Fatal(err)
	}
	cache := redis.NewClient(opts)
	defer cache.Close()
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	type site struct {
		perform func(string, string, string, bool, bool) *httptest.ResponseRecorder
		control *node.Service
		id      string
		agent   *testReleaseAgent
		db      *storeSQLite.Store
		root    string
	}
	create := func(origin string) site {
		router, db, dir, p, control := clusterFixture(t, origin, transport, true)
		cfg := p.settings
		cfg.TLSEnabled = true
		cfg.SecretsDirectory = filepath.Join(dir, "secrets")
		key := "node-admin-fixture-key"
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
		a, err := RegisterAdmin(router, cfg, auth, p, identity)
		if err != nil {
			t.Fatal(err)
		}
		RegisterAdminNodes(router, a, control)
		agent := &testReleaseAgent{status: map[string]any{"release_branch": "main", "current_sha": strings.Repeat("a", 40), "previous_sha": strings.Repeat("b", 40), "state": "success"}}
		siteService, err := sitecontrol.Open(dir, agent)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { siteService.Close() })
		RegisterSiteAdmin(router, a, siteService)
		coordinator := &release.Coordinator{Agent: agent, Verifier: &testReleaseEvidence{sha: strings.Repeat("c", 40), publishable: true}, Nodes: db.Nodes(), Control: control, Policy: release.DefaultPolicy()}
		RegisterReleaseAdmin(router, a, coordinator)
		resolver, _ := network.New(cfg.TrustedProxyNetworks)
		RegisterNodeRelease(router, cfg, resolver, control, agent)
		cookies := map[string]*http.Cookie{}
		perform := func(method, path, body string, csrf, secure bool) *httptest.ResponseRecorder {
			r := httptest.NewRequest(method, origin+"/api/v1/media/admin"+path, strings.NewReader(body))
			if secure {
				r.TLS = &tls.ConnectionState{}
			} else {
				r.TLS = nil
				r.URL.Scheme = "http"
				r.RemoteAddr = "127.0.0.1:123"
			}
			r.Header.Set("Content-Type", "application/json")
			if path == "/elevate" {
				r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
			}
			for _, cookie := range cookies {
				r.AddCookie(cookie)
			}
			if csrf && cookies[cfg.CSRFCookieName()] != nil {
				r.Header.Set("X-CSRF-Token", cookies[cfg.CSRFCookieName()].Value)
			}
			w := httptest.NewRecorder()
			router.ServeHTTP(w, r)
			for _, cookie := range w.Result().Cookies() {
				cookies[cookie.Name] = cookie
			}
			return w
		}
		if w := perform("GET", "/nodes", "", false, true); w.Code != 401 {
			t.Fatal("anonymous nodes", w.Code)
		}
		if w := perform("GET", "/nodes/release", "", false, true); w.Code != 401 {
			t.Fatal("anonymous release status", w.Code)
		}
		if w := perform("POST", "/elevate", "token="+url.QueryEscape(key), false, true); w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
		return site{perform, control, identity.ID, agent, db, dir}
	}
	m := create("https://admin-master.test")
	f := create("https://admin-follower.test")
	check := func(w *httptest.ResponseRecorder, code int) map[string]any {
		t.Helper()
		if w.Code != code {
			t.Fatalf("HTTP %d want %d: %s", w.Code, code, w.Body.String())
		}
		var value map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	masterBody := `{"role":"Master","endpoint":"https://admin-master.test","local_capacity_gib":5}`
	check(m.perform("POST", "/site/maintenance", `{"enabled":true}`, false, true), 403)
	check(m.perform("POST", "/site/maintenance", `{"enabled":true}`, true, false), 403)
	check(m.perform("POST", "/site/maintenance", `{}`, true, true), 422)
	if _, err = m.db.Database().Exec("CREATE TRIGGER site_intent_failure BEFORE INSERT ON admin_audit_log WHEN NEW.action='maintenance_change' AND NEW.result='pending' BEGIN SELECT RAISE(ABORT,'injected'); END"); err != nil {
		t.Fatal(err)
	}
	check(m.perform("POST", "/site/maintenance", `{"enabled":true}`, true, true), 500)
	if _, err = os.Stat(filepath.Join(m.root, sitecontrol.Manual)); !os.IsNotExist(err) {
		t.Fatal("failed audit changed site flags", err)
	}
	if _, err = m.db.Database().Exec("DROP TRIGGER site_intent_failure"); err != nil {
		t.Fatal(err)
	}
	if value := check(m.perform("POST", "/site/maintenance", `{"enabled":true}`, true, true), 200); value["maintenance"] != true || value["source"] != "manual" {
		t.Fatal(value)
	}
	m.agent.mu.Lock()
	m.agent.status["state"] = "running"
	m.agent.mu.Unlock()
	check(m.perform("POST", "/site/maintenance", `{"enabled":false}`, true, true), 409)
	m.agent.mu.Lock()
	m.agent.status["state"] = "failed"
	m.agent.mu.Unlock()
	if value := check(m.perform("POST", "/site/maintenance", `{"enabled":false}`, true, true), 200); value["force_open"] != true || value["maintenance"] != false {
		t.Fatal(value)
	}
	m.agent.mu.Lock()
	m.agent.status["state"] = "success"
	m.agent.mu.Unlock()
	if value := check(m.perform("POST", "/site/maintenance", `{"enabled":false}`, true, true), 200); value["force_open"] != false {
		t.Fatal(value)
	}
	check(m.perform("POST", "/nodes/promote", masterBody, false, true), 403)
	check(m.perform("POST", "/nodes/promote", masterBody, true, false), 403)
	check(m.perform("POST", "/nodes/promote", masterBody, true, true), 200)
	if value := check(m.perform("GET", "/status", "", false, true), 200); value["node_role"] != "Master" {
		t.Fatal("stale startup role", value)
	}
	check(f.perform("POST", "/nodes/promote", `{"role":"Follower","endpoint":"https://admin-follower.test"}`, true, true), 200)
	check(m.perform("POST", "/nodes/promote", masterBody, true, true), 409)
	pack := check(f.perform("POST", "/nodes/pair-package", "", true, true), 200)
	body, _ := json.Marshal(map[string]any{"package": pack})
	relation := check(m.perform("POST", "/nodes/pair", string(body), true, true), 200)["relationship_id"].(string)
	if status := check(f.perform("GET", "/status", "", false, true), 200); status["master_url"] != "https://admin-master.test" {
		t.Fatal("Follower Admin lost Master navigation", status)
	}
	if status := check(m.perform("GET", "/nodes/release", "", false, true), 200); status["can_upgrade"] != true || len(status["followers"].([]any)) != 1 {
		t.Fatal("release control cannot inspect signed Follower status", status)
	}
	check(m.perform("POST", "/nodes/release/upgrade", "", false, true), 403)
	check(m.perform("POST", "/nodes/release/upgrade", "", true, false), 403)
	check(m.perform("POST", "/nodes/release/upgrade", `{"target_sha":"unverified"}`, true, true), 400)
	check(f.perform("POST", "/nodes/release/upgrade", "", true, true), 409)
	if m.agent.starts != 0 || f.agent.starts != 0 {
		t.Fatal("unauthorized release queued")
	}
	if _, err = m.db.Database().Exec("CREATE TRIGGER release_intent_failure BEFORE INSERT ON admin_audit_log WHEN NEW.action='release_upgrade' AND NEW.result='pending' BEGIN SELECT RAISE(ABORT,'injected'); END"); err != nil {
		t.Fatal(err)
	}
	check(m.perform("POST", "/nodes/release/upgrade", "", true, true), 500)
	if m.agent.starts != 0 {
		t.Fatal("failed intent audit queued release")
	}
	if _, err = m.db.Database().Exec("DROP TRIGGER release_intent_failure"); err != nil {
		t.Fatal(err)
	}
	check(m.perform("POST", "/nodes/release/upgrade", "", true, true), 200)
	if m.agent.starts != 1 {
		t.Fatal("verified release not queued")
	}
	masterRelation, err := m.db.Nodes().Relationship(ctx, relation)
	if err != nil {
		t.Fatal(err)
	}
	followerRelation, err := f.db.Nodes().Relationship(ctx, relation)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = f.control.Call(ctx, followerRelation, "/internal/v1/cluster-update/start", map[string]any{"target_sha": strings.Repeat("c", 40), "mode": "upgrade"}); err == nil {
		t.Fatal("Follower controlled Master's updater")
	}
	if _, err = m.control.Call(ctx, masterRelation, "/internal/v1/cluster-update/start", map[string]any{"target_sha": "short", "mode": "upgrade"}); err == nil {
		t.Fatal("invalid signed release accepted")
	}
	if _, err = m.control.Call(ctx, masterRelation, "/internal/v1/cluster-update/start", map[string]any{"target_sha": strings.Repeat("c", 40), "mode": "upgrade"}); err != nil {
		t.Fatal("signed release start", err)
	}
	f.agent.busy = true
	if _, err = m.control.Call(ctx, masterRelation, "/internal/v1/cluster-update/start", map[string]any{"target_sha": strings.Repeat("c", 40), "mode": "upgrade"}); err != nil {
		t.Fatal("lost start reply is not idempotent", err)
	}
	if f.agent.starts != 1 {
		t.Fatal("Follower release replay duplicated", f.agent.starts)
	}
	check(m.perform("POST", "/nodes/"+relation+"/mode", `{"mode":"Direct"}`, true, true), 200)
	check(m.perform("POST", "/nodes/"+relation+"/resources", `{"storage_enabled":true,"storage_capacity_gib":2,"backup_enabled":true}`, true, true), 200)
	row, err := m.control.IdentityState(ctx)
	if err != nil {
		t.Fatal(err)
	}
	check(m.perform("POST", "/nodes/"+row.ID+"/resources", `{"storage_enabled":false,"storage_capacity_gib":0}`, true, true), 409)
	status := check(m.perform("GET", "/nodes", "", false, true), 200)
	relations := status["relationships"].([]any)
	rel := relations[0].(map[string]any)
	if _, ok := rel["credential"]; ok {
		t.Fatal("credential exposed")
	}
	if _, ok := rel["peer_key"]; ok {
		t.Fatal("key exposed")
	}
	if status["storage_pool"] == nil {
		t.Fatal("missing Master pool")
	}
	check(m.perform("POST", "/nodes/reinitialize", `{"confirmation":"wrong"}`, true, true), 409)
	check(m.perform("POST", "/nodes/"+relation+"/revoke", "", true, true), 200)
	pack = check(f.perform("POST", "/nodes/pair-package", "", true, true), 200)
	body, _ = json.Marshal(map[string]any{"package": pack})
	check(m.perform("POST", "/nodes/pair", string(body), true, true), 200)
	reset := check(m.perform("POST", "/nodes/reinitialize", `{"confirmation":"`+m.id+`"}`, true, true), 200)
	if reset["role"] != "Standalone" || reset["node_id"] == m.id {
		t.Fatal("identity not rotated", reset)
	}
	if value := check(m.perform("GET", "/status", "", false, true), 200); value["node_role"] != "Standalone" {
		t.Fatal("reset role stale", value)
	}
	check(m.perform("POST", "/nodes/release/rollback", "", true, true), 409)
	if _, err := m.control.SignedIdentity(ctx, strings.Repeat("a", 32)); err != nil {
		t.Fatal("rotated key unavailable", err)
	}
	check(m.perform("POST", "/nodes/reinitialize", `{"confirmation":"`+m.id+`"}`, true, true), 409)
}
