package main

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/recording"
	"github.com/wongyiuming/FrontierCloud/internal/security"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	contracts "github.com/wongyiuming/FrontierCloud/protocol"
)

func nativeHTTPFixture(t *testing.T) *gin.Engine {
	t.Helper()
	ctx := context.Background()
	dir := t.TempDir()
	settings, err := config.LoadFrom(func(key string) string {
		switch key {
		case "DATA_ROOT":
			return dir
		case "STATIC_ROOT":
			p, _ := filepath.Abs("../../static")
			return p
		case "SECRETS_DIR":
			return filepath.Join(dir, "secrets")
		}
		return ""
	})
	if err != nil {
		t.Fatal(err)
	}
	settings.NginxMedia = false
	for _, name := range []string{"media/music/artist", "media/vido", "media/lyrics", "recordings", "secrets"} {
		if err := os.MkdirAll(filepath.Join(dir, name), 0700); err != nil {
			t.Fatal(err)
		}
	}
	for name, contents := range map[string]string{"secrets/admin_key": "native-fixture-key", "secrets/metrics_token": "metrics-fixture-key", "media/music/artist/test.mp3": "native-media-bytes"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(contents), 0600); err != nil {
			t.Fatal(err)
		}
	}
	db, err := sqlitestore.Open(filepath.Join(dir, "native.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err = db.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	identity, err := node.Initialize(ctx, db.Nodes(), settings.SecretsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	mediaService, err := media.New(filepath.Join(dir, "media"), db.Media(), identity)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { mediaService.Close() })
	transport := node.NewTransport()
	t.Cleanup(func() { transport.Close() })
	control := node.NewService(db.Nodes(), identity, transport)
	root, err := os.OpenRoot(filepath.Join(dir, "recordings"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { root.Close() })
	control.ConfigureVolumes(db.Pool(), mediaService, root)
	mediaService.ConfigureCluster(db.Nodes(), db.Pool(), control)
	storage, err := recording.New(root, db.Recordings(), db.Nodes())
	if err != nil {
		t.Fatal(err)
	}
	manager := recording.NewManager(db.Recordings(), db.Karaoke(), db.Nodes(), db.Pool(), control, storage)
	policy, err := security.New(settings, db.Security())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { policy.Close() })
	if err = policy.Publish(ctx, true); err != nil {
		t.Fatal(err)
	}
	cache := redis.NewClient(&redis.Options{Addr: "127.0.0.1:1"})
	t.Cleanup(func() { cache.Close() })
	resolver, err := network.New(settings.TrustedProxyNetworks)
	if err != nil {
		t.Fatal(err)
	}
	router, closeHTTP, err := newRuntimeHTTP(settings, runtimeHTTP{Database: db, Identity: identity, Media: mediaService, Control: control, Recordings: storage, Manager: manager, Security: policy, Redis: cache, Resolver: resolver})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := closeHTTP(); err != nil {
			t.Error(err)
		}
	})
	return router
}

var namedPathParameter = regexp.MustCompile(`\{[^{}]+\}`)

func pathShape(path string) string {
	parts := strings.Split(path, "/")
	for i, v := range parts {
		if strings.HasPrefix(v, ":") || strings.HasPrefix(v, "*") {
			parts[i] = "{}"
		}
	}
	return namedPathParameter.ReplaceAllString(strings.Join(parts, "/"), "{}")
}
func TestProductionHTTPRegistrationCoversEffectiveReferenceInventory(t *testing.T) {
	router := nativeHTTPFixture(t)
	available := map[string]bool{}
	for _, r := range router.Routes() {
		available[r.Method+" "+pathShape(r.Path)] = true
	}
	b, err := os.ReadFile("../../protocol/v2/http-routes.json")
	if err != nil {
		t.Fatal(err)
	}
	var rows []struct {
		Path    string
		Methods []string
	}
	if err = json.Unmarshal(b, &rows); err != nil {
		t.Fatal(err)
	}
	for _, r := range rows {
		for _, method := range r.Methods {
			if !available[method+" "+pathShape(r.Path)] {
				t.Errorf("missing effective route: %s %s", method, r.Path)
			}
		}
	}
	var schema struct{ Paths map[string]map[string]any }
	if err = json.Unmarshal(contracts.OpenAPI, &schema); err != nil {
		t.Fatal(err)
	}
	for path, operations := range schema.Paths {
		for method := range operations {
			if !available[strings.ToUpper(method)+" "+pathShape(path)] {
				t.Errorf("published schema has no native implementation: %s %s", method, path)
			}
		}
	}
}

func TestProductionHTTPWiringServesMediaAndEnforcesOperationalAuthentication(t *testing.T) {
	router := nativeHTTPFixture(t)
	for _, tc := range []struct {
		path     string
		status   int
		contains string
	}{
		{"/health/live", 200, "healthy"},
		{"/api/v1/media/catalog/categories?media_type=music", 200, "entries"},
		{"/api/v1/media/stream?file_path=music/artist/test.mp3", 200, "native-media-bytes"},
		{"/docs", 401, ""}, {"/redoc", 401, ""}, {"/openapi.json", 401, ""}, {"/metrics", 404, ""},
	} {
		r := httptest.NewRequest("GET", "http://localhost"+tc.path, nil)
		r.RemoteAddr = "127.0.0.1:3456"
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		if w.Code != tc.status || !strings.Contains(w.Body.String(), tc.contains) {
			t.Fatalf("%s HTTP %d: %s", tc.path, w.Code, w.Body.String())
		}
	}
	r := httptest.NewRequest("GET", "http://localhost/metrics", nil)
	r.Header.Set("Authorization", "Bearer metrics-fixture-key")
	r.RemoteAddr = "127.0.0.1:3456"
	w := httptest.NewRecorder()
	router.ServeHTTP(w, r)
	if w.Code != 200 || !strings.Contains(w.Body.String(), "frontiercloud_http_requests_total") {
		t.Fatal(w.Code, w.Body.String())
	}
}
