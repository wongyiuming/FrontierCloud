package httpapi

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"io"
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
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestGlobalDownloadAdminRedisPrimaryDirectRelayAndOffline(t *testing.T) {
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
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	master := nativeRecordingFixture(t, "https://download-master.test", "Master", transport)
	direct := nativeRecordingFixture(t, "https://download-direct.test", "Follower", transport)
	relay := nativeRecordingFixture(t, "https://download-relay.test", "Follower", transport)
	for i, follower := range []*recordingNode{&direct, &relay} {
		pack, err := follower.control.CreatePair(ctx, store.NodeAudit{})
		if err != nil {
			t.Fatal(err)
		}
		id, err := master.control.ImportPair(ctx, pack, store.NodeAudit{})
		if err != nil {
			t.Fatal(err)
		}
		cfg := store.ResourceConfiguration{}
		cfg.Storage.Enabled, cfg.Storage.Allocation = true, 5*store.GiB
		if err := master.db.Pool().ConfigureMember(ctx, follower.id, cfg, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		mode := "Direct"
		if i == 1 {
			mode = "Relay"
		}
		if err := master.db.Nodes().SetRelationshipMode(ctx, id, mode, false, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		relation, err := master.db.Nodes().Relationship(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		if err := master.control.Tick(ctx, relation); err != nil {
			t.Fatal(err)
		}
	}
	tickets := []media.UploadTicket{}
	for _, site := range []string{"primary", "direct", "relay"} {
		ticket, err := master.public.media.ReserveMasterUpload(ctx, site+".mp3", "music/下载%_!/"+site, "", site, 10, 255, store.AdminAudit{})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := master.public.media.UploadMasterBytes(ctx, ticket.ID, strings.NewReader("ID3payload"), store.AdminAudit{}); err != nil {
			t.Fatal(err)
		}
		tickets = append(tickets, ticket)
	}
	cfg := master.public.settings
	if cfg.NginxMedia {
		t.Fatal("test must prove native Relay attachment without Nginx")
	}
	cfg.SecretsDirectory = filepath.Join(master.dir, "secrets")
	key := "download-admin-fixture-key"
	if err := os.WriteFile(filepath.Join(cfg.SecretsDirectory, "admin_key"), []byte(key), 0600); err != nil {
		t.Fatal(err)
	}
	identity, err := node.Initialize(ctx, master.db.Nodes(), cfg.SecretsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	auth, err := admin.New(cfg, admin.NewRedisCache(client), master.db.Admin())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := RegisterAdmin(master.router, cfg, auth, master.public, identity); err != nil {
		t.Fatal(err)
	}
	cookies := []*http.Cookie{}
	perform := func(method, target, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "https://download-master.test"+target, strings.NewReader(body))
		r.TLS = &tls.ConnectionState{}
		if body != "" {
			r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		}
		for _, cookie := range cookies {
			r.AddCookie(cookie)
		}
		w := httptest.NewRecorder()
		master.router.ServeHTTP(w, r)
		return w
	}
	target := func(paths ...string) string {
		raw, err := json.Marshal(paths)
		if err != nil {
			t.Fatal(err)
		}
		return "/api/v1/media/admin/download?" + url.Values{"paths": {string(raw)}}.Encode()
	}
	if w := perform("GET", target(tickets[1].Path), ""); w.Code != 401 {
		t.Fatal("anonymous download", w.Code)
	}
	w := perform("POST", "/api/v1/media/admin/elevate", "token="+url.QueryEscape(key))
	if w.Code != 200 {
		t.Fatal("elevate", w.Code, w.Body.String())
	}
	cookies = w.Result().Cookies()
	for _, ticket := range tickets {
		w = perform("GET", target(ticket.Path), "")
		if w.Code != 200 || w.Body.String() != "ID3payload" || !strings.HasPrefix(w.Header().Get("Content-Disposition"), "attachment;") || w.Header().Get("Cache-Control") != "private, no-store" || w.Header().Get("Location") != "" || w.Header().Get("X-Accel-Redirect") != "" || w.Header().Get("Content-Length") != "10" {
			t.Fatal("attachment must stream through authenticated Master", ticket.Site, w.Code, w.Header(), w.Body.String())
		}
	}
	w = perform("GET", target("music/下载%_!", tickets[1].Path), "")
	if w.Code != 200 || w.Header().Get("Content-Type") != "application/zip" {
		t.Fatal("archive response", w.Code, w.Header(), w.Body.String())
	}
	archive, err := zip.NewReader(bytes.NewReader(w.Body.Bytes()), int64(w.Body.Len()))
	if err != nil {
		t.Fatal(err)
	}
	if len(archive.File) != 3 {
		t.Fatal("overlap not deduplicated", len(archive.File))
	}
	names := map[string]bool{}
	for _, entry := range archive.File {
		reader, err := entry.Open()
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(reader)
		reader.Close()
		if err != nil || string(body) != "ID3payload" || entry.Method != zip.Store {
			t.Fatal("archive content", entry.Name, err)
		}
		names[entry.Name] = true
	}
	for _, ticket := range tickets {
		if !names[ticket.Path] {
			t.Fatal("missing member", ticket.Path)
		}
	}
	// The heartbeat remains fresh: transport failure must become a retryable
	// JSON error before bytes, never an empty successful attachment/redirect.
	delete(transport.routers, "https://download-direct.test")
	for _, paths := range [][]string{{tickets[1].Path}, {"music/下载%_!/direct"}} {
		w = perform("GET", target(paths...), "")
		if w.Code != 503 || w.Header().Get("Retry-After") != "30" || !strings.HasPrefix(w.Header().Get("Content-Type"), "application/json") || w.Header().Get("Content-Disposition") != "" || w.Header().Get("Content-Length") != "" {
			t.Fatal("offline placement falsely complete", w.Code, w.Header(), w.Body.String())
		}
	}
	w = perform("GET", target("music/../escape"), "")
	if w.Code != 400 {
		t.Fatal("path traversal", w.Code, w.Body.String())
	}
}
