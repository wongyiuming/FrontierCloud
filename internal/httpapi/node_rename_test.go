package httpapi

import (
	"context"
	"errors"
	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestNodeOwnedRenameAuthenticatesUpstreamPreservesIDsAndRefundsRenamedUpload(t *testing.T) {
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	_, masterDB, _, _, master := clusterFixture(t, "https://rename-master.test", transport, true)
	router, followerDB, _, public, follower := clusterFixture(t, "https://rename-follower.test", transport, true)
	if _, err := master.Promote(ctx, "Master", "https://rename-master.test", 5*store.GiB, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err := follower.Promote(ctx, "Follower", "https://rename-follower.test", 0, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	pack, err := follower.CreatePair(ctx, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	relID, err := master.ImportPair(ctx, pack, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	rel, err := masterDB.Nodes().Relationship(ctx, relID)
	if err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled = true
	cfg.Storage.Allocation = 5 * store.GiB
	if err := masterDB.Pool().ConfigureMember(ctx, rel.PeerID, cfg, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := master.Tick(ctx, rel); err != nil {
		t.Fatal(err)
	}
	old, target, op := "music/旧目录%_!", "music/新目录%_!", strings.Repeat("4", 32)
	object := store.MediaObject{ID: strings.Repeat("4", 64), Path: old + "/song.mp3", Kind: "audio"}
	if _, err := public.media.OwnedUpload(ctx, relID, object, 10, strings.NewReader("ID3payload"), store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	route := "/internal/v1/storage-control/directory-rename"
	for _, tc := range []struct {
		origin, body string
		code         int
	}{{"http://rename-follower.test", "{}", 403}, {"https://rename-follower.test", "not-json", 401}, {"https://rename-follower.test", strings.Repeat("x", node.MaxControlBytes+1), 413}} {
		w := request(router, "POST", tc.origin+route, tc.body)
		if w.Code != tc.code {
			t.Fatal("unauthenticated rename", w.Code, w.Body.String())
		}
	}
	for range 2 {
		if err := master.RenameStorage(ctx, relID, old, target, op); err != nil {
			t.Fatal(err)
		}
	}
	actual, err := followerDB.Media().ObjectByID(ctx, object.ID)
	if err != nil || actual == nil || actual.Path != target+"/song.mp3" {
		t.Fatal(actual, err)
	}
	if _, err := master.Call(ctx, rel, "/internal/v1/storage/"+object.ID+"/stat", map[string]any{"path": actual.Path}); err != nil {
		t.Fatal("stat renamed upload", err)
	}
	if _, err := master.Call(ctx, rel, route, map[string]any{"old_path": target, "new_path": "vido/illegal"}); err == nil {
		t.Fatal("cross-root rename allowed")
	}
	// A Python Master can omit operation_id; the native Follower creates its own
	// durable ID and responds with the established path/status contract.
	if _, err := master.Call(ctx, rel, route, map[string]any{"old_path": target, "new_path": "music/兼容目录"}); err != nil {
		t.Fatal("legacy body rejected", err)
	}
	actual, err = followerDB.Media().ObjectByID(ctx, object.ID)
	if err != nil || actual == nil || actual.Path != "music/兼容目录/song.mp3" {
		t.Fatal(actual, err)
	}
	if err := public.media.OwnedDelete(ctx, relID, object.ID, actual.Path, 10, store.NodeAudit{}); err != nil {
		t.Fatal("post-rename deletion", err)
	}
	var used, reserved int64
	if err := followerDB.Database().QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", rel.PeerID).Scan(&used, &reserved); err != nil || used != 0 || reserved != 0 {
		t.Fatal("post-rename quota refund", used, reserved, err)
	}
	if _, err := followerDB.Database().Exec("UPDATE node_relationships SET state='revoked' WHERE relationship_id=?", relID); err != nil {
		t.Fatal(err)
	}
	if err := master.RenameStorage(ctx, relID, old, target, op); err == nil {
		t.Fatal("revoked upstream replay accepted")
	}
	if err := follower.RenameStorage(ctx, relID, old, target, op); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("Follower dispatched Master rename", err)
	}
}
