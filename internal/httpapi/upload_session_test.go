package httpapi

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/admin"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestMasterSessionHTTPPrimaryDirectRelayFinalizeCancelAndPool(t *testing.T) {
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	mr, db, _, public, master := clusterFixture(t, "https://master.test", transport, false)
	fr, fdb, _, _, follower := clusterFixture(t, "https://follower.test", transport, true)
	if _, err := master.Promote(ctx, "Master", "https://master.test", 10*store.GiB, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err := follower.Promote(ctx, "Follower", "https://follower.test", 0, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	pair, err := follower.CreatePair(ctx, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	relID, err := master.ImportPair(ctx, pair, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	rel, _ := db.Nodes().Relationship(ctx, relID)
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled, cfg.Storage.Allocation = true, 5*store.GiB
	if err := db.Pool().ConfigureMember(ctx, rel.PeerID, cfg, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := master.Tick(ctx, rel); err != nil {
		t.Fatal(err)
	}
	resolver, _ := network.New(nil)
	a := &Admin{public: public, settings: public.settings, network: resolver}
	// Authentication and CSRF are tested through RegisterAdmin with real Redis.
	// This fixture exercises the business handlers using an established session.
	g := mr.Group("/api/v1/media/admin", func(c *gin.Context) { c.Set("admin_session", admin.Session{Hash: strings.Repeat("1", 64)}); c.Next() })
	g.POST("/upload/session", a.reserveUpload)
	g.PUT("/upload/session/:upload/bytes", a.uploadBytes)
	g.POST("/upload/session/:upload/finalize", a.finalizeUpload)
	g.DELETE("/upload/session/:upload", a.cancelUpload)
	g.POST("/upload/item", func(c *gin.Context) { a.upload(c, false) })
	g.GET("/storage-pool", a.storagePool)
	perform := func(method, url, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "https://master.test"+url, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		mr.ServeHTTP(w, r)
		return w
	}
	for _, site := range []string{"primary", "direct", "relay"} {
		if site != "primary" {
			mode := "Direct"
			if site == "relay" {
				mode = "Relay"
			}
			if err := db.Nodes().SetRelationshipMode(ctx, relID, mode, false, store.NodeAudit{}); err != nil {
				t.Fatal(err)
			}
		}
		body, _ := json.Marshal(map[string]any{"site_type": site, "target_dir": "music/Upload-" + site, "filename": "song.mp3", "size_bytes": 10})
		w := perform("POST", "/api/v1/media/admin/upload/session", string(body))
		var ticket media.UploadTicket
		if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &ticket) != nil || ticket.Site != site {
			t.Fatal("ticket", site, w.Code, w.Body.String())
		}
		if site == "direct" {
			r := httptest.NewRequest("PUT", ticket.URL, strings.NewReader("ID3payload"))
			r.Header.Set("Origin", "https://master.test")
			w = httptest.NewRecorder()
			fr.ServeHTTP(w, r)
			if w.Code != 200 {
				t.Fatal("direct upload", w.Code, w.Body.String())
			}
			// A mode switch never redirects an existing placement to another owner.
			if err := db.Nodes().SetRelationshipMode(ctx, relID, "Relay", false, store.NodeAudit{}); err != nil {
				t.Fatal(err)
			}
			w = perform("POST", "/api/v1/media/admin/upload/session/"+ticket.ID+"/finalize", `{"object_id":"forged","etag":"fake"}`)
		} else {
			w = perform("PUT", ticket.URL, "ID3payload")
		}
		if w.Code != 200 || !strings.Contains(w.Body.String(), ticket.MediaID) {
			t.Fatal("publication", site, w.Code, w.Body.String())
		}
		w = perform("POST", "/api/v1/media/admin/upload/session/"+ticket.ID+"/finalize", "")
		if w.Code != 200 {
			t.Fatal("idempotent completion", w.Code, w.Body.String())
		}
		w = perform("DELETE", "/api/v1/media/admin/upload/session/"+ticket.ID, "")
		if w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
		if _, err := db.Pool().Resource(ctx, ticket.MediaID); err != nil {
			t.Fatal("cancel removed completed catalog", err)
		}
	}
	body := `{"site_type":"relay","target_dir":"music/CancelRemote","filename":"cancel.mp3","size_bytes":10}`
	w := perform("POST", "/api/v1/media/admin/upload/session", body)
	var ticket media.UploadTicket
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &ticket) != nil {
		t.Fatal(w.Code, w.Body.String())
	}
	v, err := db.Pool().Upload(ctx, ticket.ID)
	if err != nil {
		t.Fatal(err)
	}
	// Simulate physical upload followed by a lost response before Master commit.
	if _, err := master.UploadStorage(ctx, v, strings.NewReader("ID3payload")); err != nil {
		t.Fatal(err)
	}
	delete(transport.routers, "https://follower.test")
	w = perform("DELETE", "/api/v1/media/admin/upload/session/"+ticket.ID, "")
	if w.Code == 200 {
		t.Fatal("offline cancellation refunded uncertain bytes")
	}
	if _, err := db.Pool().Upload(ctx, ticket.ID); err != nil {
		t.Fatal("reservation lost", err)
	}
	transport.routers["https://follower.test"] = fr
	w = perform("DELETE", "/api/v1/media/admin/upload/session/"+ticket.ID, "")
	if w.Code != 200 {
		t.Fatal("physical cleanup", w.Code, w.Body.String())
	}
	object, err := fdb.Media().ObjectByID(ctx, ticket.MediaID)
	if err != nil || object != nil {
		t.Fatal("Follower bytes retained", object, err)
	}
	var used, reserved int64
	if err := fdb.Database().QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members").Scan(&used, &reserved); err != nil || used != 20 || reserved != 0 {
		t.Fatal("Follower quota", used, reserved, err)
	}
	w = perform("GET", "/api/v1/media/admin/storage-pool", "")
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"member_id":"auto"`) || !strings.Contains(w.Body.String(), `"reserved_bytes":0`) {
		t.Fatal("storage pool", w.Code, w.Body.String())
	}
	w = perform("POST", "/api/v1/media/admin/upload/item", "not-a-multipart-body")
	if w.Code != 409 {
		t.Fatal("Master legacy upload bypass", w.Code, w.Body.String())
	}
}
