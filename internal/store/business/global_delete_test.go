package business_test

import (
	"context"
	"database/sql"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestGlobalDeleteDurablePendingConcurrentRefundAndExactPaths(t *testing.T) {
	db := database(t)
	d := db.(interface{ Database() *sql.DB }).Database()
	ctx := context.Background()
	row, err := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "encrypted-fixture", CreatedAt: time.Now().Unix()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Nodes().PromoteIdentity(ctx, store.NodePromotion{Role: "Master", Endpoint: "https://master.test", Allocation: 5 * store.GiB, PhysicalFree: 10 * store.GiB}, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	rel := store.Relationship{ID: strings.Repeat("6", 32), PeerID: strings.Repeat("7", 32), Endpoint: "https://follower.test", PublicKey: strings.Repeat("A", 43), Credential: "encrypted-fixture", Direction: "downstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: time.Now().Unix()}
	if err := db.Nodes().PrepareRelationship(ctx, rel, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := db.Nodes().ActivateRelationship(ctx, rel.ID, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled, cfg.Storage.Allocation = true, 5*store.GiB
	if err := db.Pool().ConfigureMember(ctx, rel.PeerID, cfg, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := db.Nodes().RecordHeartbeat(ctx, rel.ID, true, 1, map[string]any{"storage": map[string]any{"physical_free_bytes": 10 * store.GiB}}, time.Now().Unix()); err != nil {
		t.Fatal(err)
	}
	uploads := []store.UploadReservation{}
	t.Cleanup(func() {
		for _, v := range uploads {
			for _, table := range []string{"global_media_objects", "cluster_upload_sessions", "media_objects", "media_playback_events", "media_playback_stats", "media_lyric_links"} {
				d.Exec("DELETE FROM "+table+" WHERE media_id=?", v.MediaID)
			}
		}
		for _, id := range []string{row.ID, rel.PeerID} {
			for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
				d.Exec("DELETE FROM "+table+" WHERE member_id=?", id)
			}
		}
		d.Exec("DELETE FROM node_relationships WHERE relationship_id=?", rel.ID)
		d.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", row.Role, row.Endpoint)
		d.Exec("DELETE FROM admin_audit_log WHERE request_id=?", "native-delete-test")
	})
	for _, name := range []string{"music/NativeDeletion/song.mp3", "music/nativedeletion/song.mp3", "music/NativeDeletion%_/song.mp3"} {
		v, err := db.Pool().ReserveUpload(ctx, name, "relay", 10, 10*store.GiB, store.AdminAudit{})
		if err != nil {
			t.Fatal(err)
		}
		uploads = append(uploads, v)
		if _, err := db.Pool().FinalizeUpload(ctx, v.ID, v.MediaID, 10, `"digest"`, store.AdminAudit{}); err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Pool().CompleteGlobalDelete(ctx, uploads[0].MediaID, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("active placement refunded", err)
	}
	selected, err := db.Pool().PrepareGlobalDelete(ctx, []store.DeleteItem{{Path: "music/NativeDeletion", Directory: true}}, store.AdminAudit{RequestID: "native-delete-test"})
	if err != nil || len(selected) != 1 || selected[0].ID != uploads[0].MediaID {
		t.Fatal("case/prefix selection", selected, err)
	}
	if _, err := db.Pool().ReserveUpload(ctx, uploads[0].Path, "relay", 10, 10*store.GiB, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("pending path reused", err)
	}
	var used int64
	if err := d.QueryRow("SELECT used_bytes FROM cluster_storage_members WHERE member_id=?", rel.PeerID).Scan(&used); err != nil || used != 30 {
		t.Fatal("prepare refunded capacity", used, err)
	}
	var wg sync.WaitGroup
	for range 16 {
		wg.Go(func() {
			if err := db.Pool().CompleteGlobalDelete(ctx, uploads[0].MediaID, store.AdminAudit{RequestID: "native-delete-test"}); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if err := d.QueryRow("SELECT used_bytes FROM cluster_storage_members WHERE member_id=?", rel.PeerID).Scan(&used); err != nil || used != 20 {
		t.Fatal("refund duplicated", used, err)
	}
	var count int
	if err := d.QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='global-media-delete-completed' AND request_id=?", "native-delete-test").Scan(&count); err != nil || count != 1 {
		t.Fatal("audit duplicated", count, err)
	}
	for _, v := range uploads[1:] {
		if _, err := db.Pool().Resource(ctx, v.MediaID); err != nil {
			t.Fatal("unselected sibling deleted", err)
		}
	}
}
