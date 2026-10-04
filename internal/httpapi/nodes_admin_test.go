package httpapi

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/admin"
	"github.com/wongyiuming/FrontierCloud/internal/diagnostics"
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
		RegisterOperational(router, a)
		diagnosticResolver, _ := network.New(cfg.TrustedProxyNetworks)
		RegisterPlaybackDiagnostics(router, cfg, diagnosticResolver, control, a, diagnostics.New())
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
		if w := perform("GET", "/nodes/observability", "", false, true); w.Code != 401 {
			t.Fatal("anonymous observations", w.Code)
		}
		if w := perform("GET", "/playback-continuity-diagnostics", "", false, true); w.Code != 401 {
			t.Fatal("anonymous diagnostics", w.Code)
		}
		for _, path := range []string{"/docs", "/redoc", "/openapi.json"} {
			w := request(router, "GET", origin+path, "")
			if w.Code != 401 {
				t.Fatal("anonymous documentation", path, w.Code)
			}
		}
		if w := perform("GET", "/nodes/release", "", false, true); w.Code != 401 {
			t.Fatal("anonymous release status", w.Code)
		}
		if w := perform("POST", "/elevate", "token="+url.QueryEscape(key), false, true); w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
		for _, path := range []string{"/docs", "/redoc", "/openapi.json"} {
			r := httptest.NewRequest("GET", origin+path, nil)
			for _, cookie := range cookies {
				r.AddCookie(cookie)
			}
			w := httptest.NewRecorder()
			router.ServeHTTP(w, r)
			if w.Code != 200 || !strings.Contains(w.Body.String(), "/openapi.json") && path != "/openapi.json" {
				t.Fatal(path, w.Code, w.Body.String())
			}
			if w.Header().Get("Cache-Control") != "no-store" {
				t.Fatal("documentation cacheable", path)
			}
			if path == "/openapi.json" {
				var schema map[string]any
				if err := json.Unmarshal(w.Body.Bytes(), &schema); err != nil {
					t.Fatal(err)
				}
				if schema["openapi"] != "3.1.0" || len(schema["paths"].(map[string]any)) < 80 {
					t.Fatal("incomplete schema")
				}
				components := schema["components"].(map[string]any)["schemas"].(map[string]any)
				if components["Promotion"] == nil || components["AuthPayload"] == nil {
					t.Fatal("typed API schemas missing")
				}
			}
		}
		if w := request(router, "POST", origin+"/api/v1/media/playback-continuity-diagnostics", `{"diagnostic_id":"admin-case","stage":"pause","sample":{"cookie":"private","paused":true}}`); w.Code != 202 {
			t.Fatal(w.Code, w.Body.String())
		}
		if w := perform("GET", "/playback-continuity-diagnostics", "", false, true); w.Code != 200 || !strings.Contains(w.Body.String(), "admin-case") || strings.Contains(w.Body.String(), "private") {
			t.Fatal(w.Code, w.Body.String())
		}
		if w := perform("DELETE", "/playback-continuity-diagnostics", "", false, true); w.Code != 403 {
			t.Fatal("missing diagnostic clear CSRF", w.Code)
		}
		if w := perform("DELETE", "/playback-continuity-diagnostics", "", true, true); w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
		if w := perform("GET", "/playback-continuity-diagnostics", "", false, true); w.Code != 200 || !strings.Contains(w.Body.String(), `"reports":[]`) {
			t.Fatal("diagnostic clear ineffective", w.Code, w.Body.String())
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
	f.agent.mu.Lock()
	f.agent.unavailable = true
	f.agent.mu.Unlock()
	if value, probeErr := m.control.Call(ctx, masterRelation, "/internal/v1/cluster-update/status", map[string]any{}); probeErr == nil || !strings.HasPrefix(probeErr.Error(), "control status 503:") || len(value) != 0 {
		t.Fatal("agent socket gap was reported as successful capability/profile observation", value, probeErr)
	}
	f.agent.mu.Lock()
	f.agent.unavailable = false
	f.agent.mu.Unlock()
	if _, probeErr := m.control.Call(ctx, masterRelation, "/internal/v1/cluster-update/status", map[string]any{}); probeErr != nil {
		t.Fatal("recovered agent status unavailable", probeErr)
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
	// Exercise the real authenticated RPC boundary with different Python/Go
	// artifacts. The Master must forward the complete manifest without a SHA.
	manifest := &release.Manifest{Format: "frontiercloud-release-manifest", Version: 1, ReleaseVersion: "2.0.0", Protocol: 2, SchemaGeneration: 2, Artifacts: map[string]release.Artifact{
		"main":     {Kind: "git-archive", CommitSHA: strings.Repeat("c", 40), SourceSHA: strings.Repeat("e", 40), TreeSHA: strings.Repeat("f", 40)},
		"gin_main": {Kind: "git-archive", CommitSHA: strings.Repeat("d", 40), SourceSHA: strings.Repeat("a", 40), TreeSHA: strings.Repeat("b", 40)},
	}}
	encodedManifest, _ := manifest.Wire()
	var whole map[string]any
	manifestDecoder := json.NewDecoder(bytes.NewReader(encodedManifest))
	manifestDecoder.UseNumber()
	if err = manifestDecoder.Decode(&whole); err != nil {
		t.Fatal(err)
	}
	f.agent.busy = false
	f.agent.capabilities = []string{release.ManifestCapability}
	manifestID, _ := manifest.ID()
	manifestRequest := map[string]any{"release_manifest": whole, "mode": "upgrade"}
	if _, err = f.control.Call(ctx, followerRelation, "/internal/v1/cluster-update/start", manifestRequest); err == nil {
		t.Fatal("Follower controlled Master's manifest release")
	}
	ack, err := m.control.Call(ctx, masterRelation, "/internal/v1/cluster-update/start", manifestRequest)
	if err != nil || ack["release_id"] != manifestID || f.agent.lastManifest == nil {
		t.Fatal("whole manifest forwarding", ack, err)
	}
	f.agent.busy = true
	ack, err = m.control.Call(ctx, masterRelation, "/internal/v1/cluster-update/start", manifestRequest)
	if err != nil || ack["release_id"] != manifestID || f.agent.starts != 2 {
		t.Fatal("manifest replay not idempotent", ack, err, f.agent.starts)
	}
	manifestRequest["target_sha"] = strings.Repeat("c", 40)
	if _, err = m.control.Call(ctx, masterRelation, "/internal/v1/cluster-update/start", manifestRequest); err == nil {
		t.Fatal("Master selected artifact alongside manifest")
	}
	delete(manifestRequest, "target_sha")
	whole["protocol"] = 3
	if _, err = m.control.Call(ctx, masterRelation, "/internal/v1/cluster-update/start", manifestRequest); err == nil {
		t.Fatal("unsupported manifest protocol accepted")
	}
	whole["protocol"] = 2
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
	if err = m.control.Tick(ctx, masterRelation); err != nil {
		t.Fatal(err)
	}
	observations := check(m.perform("GET", "/nodes/observability", "", false, true), 200)
	if observations["role"] != "Master" || len(observations["members"].([]any)) != 2 {
		t.Fatal("missing authoritative member observations", observations)
	}
	for _, raw := range observations["members"].([]any) {
		member := raw.(map[string]any)
		if member["sync"].(map[string]any)["storage"] != "effective" {
			t.Fatal("heartbeat resource state not observed", member)
		}
		if member["member_kind"] == "MasterLocal" && member["connection"].(map[string]any)["status"] != "local" {
			t.Fatal(member)
		}
	}
	pool := status["storage_pool"].(map[string]any)
	if pool["current_allocated_bytes"] != pool["allocated_bytes"] || pool["project_used_bytes"] != pool["used_bytes"] || pool["physical_total_bytes"].(float64) <= 0 {
		t.Fatal("four capacity facts missing", pool)
	}
	var totalPhysical, totalFree, allocation, used float64
	for _, raw := range pool["members"].([]any) {
		member := raw.(map[string]any)
		if member["member_kind"] == "Auto" {
			continue
		}
		totalPhysical += member["physical_total_bytes"].(float64)
		totalFree += member["physical_free_bytes"].(float64)
		allocation += member["current_allocated_bytes"].(float64)
		used += member["project_used_bytes"].(float64)
	}
	if totalPhysical != pool["physical_total_bytes"] || totalFree != pool["physical_free_bytes"] || allocation != pool["current_allocated_bytes"] || used != pool["project_used_bytes"] {
		t.Fatal("synthetic Auto member changed aggregate capacity", pool)
	}
	followerView := check(f.perform("GET", "/nodes/observability", "", false, true), 200)
	if followerView["role"] != "Follower" || len(followerView["members"].([]any)) != 0 || len(followerView["relationships"].([]any)) != 1 {
		t.Fatal(followerView)
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
	if value := check(m.perform("GET", "/nodes/observability", "", false, true), 200); value["role"] != "Standalone" || len(value["members"].([]any)) != 0 {
		t.Fatal("reset observations stale", value)
	}
	check(m.perform("POST", "/nodes/release/rollback", "", true, true), 409)
	if _, err := m.control.SignedIdentity(ctx, strings.Repeat("a", 32)); err != nil {
		t.Fatal("rotated key unavailable", err)
	}
	check(m.perform("POST", "/nodes/reinitialize", `{"confirmation":"`+m.id+`"}`, true, true), 409)
}
