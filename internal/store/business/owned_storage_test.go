package business_test

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestOwnedStorageQuotaReservationConcurrentPublicationAndCleanup(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	pool, nodes := db.Pool(), db.Nodes()
	row, err := nodes.InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "encrypted-fixture"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = sqlDB.Exec("UPDATE node_identity SET `role`='Follower',endpoint='https://owned.test' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	relID, nonce := "f0"+strings.Repeat("1", 30), "f0"+strings.Repeat("2", 30)
	object := store.MediaObject{ID: "f0" + strings.Repeat("3", 62), Path: "music/NativeOwnedQuota/song.mp3", Kind: "audio"}
	t.Cleanup(func() {
		sqlDB.Exec("DELETE FROM media_objects WHERE media_id=?", object.ID)
		sqlDB.Exec("DELETE FROM cluster_upload_sessions WHERE storage_member_id=?", row.ID)
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			sqlDB.Exec("DELETE FROM "+table+" WHERE member_id=?", row.ID)
		}
		sqlDB.Exec("DELETE FROM node_relationships WHERE relationship_id=?", relID)
		sqlDB.Exec("DELETE FROM node_pair_packages WHERE nonce=?", nonce)
		sqlDB.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", row.Role, row.Endpoint)
	})
	now := time.Now().Unix()
	pair := store.PairPackage{Nonce: nonce, TokenHash: strings.Repeat("f", 64), ExpiresAt: now + 300}
	if _, err = nodes.IssuePair(ctx, pair, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	relation := store.Relationship{ID: relID, PeerID: "f0" + strings.Repeat("4", 30), Endpoint: "https://master.test", PublicKey: strings.Repeat("A", 43), Credential: "encrypted-credential", Direction: "upstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: now}
	if err = nodes.ConsumePair(ctx, pair, relation, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err = nodes.ActivateRelationship(ctx, relID, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled = true
	cfg.Storage.Allocation = 10 * store.GiB
	if err = pool.AcceptFollowerConfiguration(ctx, relID, cfg, 5*store.GiB); err != nil {
		t.Fatal(err)
	}
	var group sync.WaitGroup
	var successes atomic.Int32
	winning := make(chan string, 16)
	failures := make(chan error, 32)
	for i := range 16 {
		group.Go(func() {
			operation := fmt.Sprintf("%032x", i+600)
			err := pool.ReserveOwnedUpload(ctx, operation, relID, object, 3*store.GiB, 5*store.GiB, store.NodeAudit{})
			if err == nil {
				successes.Add(1)
				winning <- operation
			} else if !errors.Is(err, store.ErrNodeState) && !errors.Is(err, store.ErrStorageCapacity) {
				failures <- err
			}
		})
	}
	group.Wait()
	if successes.Load() != 1 {
		t.Fatalf("same owned object reserved %d times", successes.Load())
	}
	operation := <-winning
	other := store.MediaObject{ID: "f0" + strings.Repeat("5", 62), Path: "music/NativeOwnedQuota/other.mp3", Kind: "audio"}
	if err = pool.ReserveOwnedUpload(ctx, strings.Repeat("6", 32), relID, other, 2*store.GiB, 5*store.GiB, store.NodeAudit{}); !errors.Is(err, store.ErrStorageCapacity) {
		t.Fatal("physical reservation double spend", err)
	}
	if err = nodes.RevokeRelationship(ctx, relID, false, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("reserved Follower revoked", err)
	}
	if err = pool.CompleteOwnedUpload(ctx, operation, object, 3*store.GiB-1, `"etag"`, store.GiB, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("signed size mismatch published", err)
	}
	for range 16 {
		group.Go(func() {
			if err := pool.CompleteOwnedUpload(ctx, operation, object, 3*store.GiB, `"etag"`, store.GiB, store.NodeAudit{RequestID: "owned-quota-publication"}); err != nil {
				failures <- err
			}
		})
	}
	group.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	var used, reserved int64
	if err = sqlDB.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", row.ID).Scan(&used, &reserved); err != nil || used != 3*store.GiB || reserved != 0 {
		t.Fatal("exact accounting", used, reserved, err)
	}
	var count int
	if err = sqlDB.QueryRow("SELECT COUNT(*) FROM node_audit WHERE action='storage-upload-published' AND detail LIKE '%owned-quota-publication%'").Scan(&count); err != nil || count != 1 {
		t.Fatal("idempotent node audit", count, err)
	}
	if err = pool.ReleaseOwnedUpload(ctx, operation, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("published placement discarded", err)
	}
	cleanup := "f0" + strings.Repeat("7", 30)
	if err = pool.ReserveOwnedUpload(ctx, cleanup, relID, other, 10, 5*store.GiB, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	old, target := "music/NativeOwnedQuota", "music/NativeOwnedQuotaRenamed"
	if err := pool.CheckOwnedRename(ctx, relID, old, target); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("renamed directory with live upload", err)
	}
	for range 2 {
		if err = pool.ReleaseOwnedUpload(ctx, cleanup, store.NodeAudit{}); err != nil {
			t.Fatal("cleanup not idempotent", err)
		}
	}
	renameID := "f0" + strings.Repeat("9", 30)
	for range 16 {
		group.Go(func() {
			if err := pool.CompleteOwnedRename(ctx, relID, old, target, renameID, store.NodeAudit{RequestID: "owned-quota-rename"}); err != nil {
				t.Error(err)
			}
		})
	}
	group.Wait()
	if err := sqlDB.QueryRow("SELECT COUNT(*) FROM node_audit WHERE action='storage-directory-renamed' AND detail LIKE '%owned-quota-rename%'").Scan(&count); err != nil || count != 1 {
		t.Fatal("rename duplicate audit", count, err)
	}
	actual, err := db.Media().ObjectByID(ctx, object.ID)
	if err != nil || actual == nil || actual.Path != target+"/song.mp3" {
		t.Fatal("rename lost identity", actual, err)
	}
	object.Path = actual.Path
	var ledgerPath string
	if err := sqlDB.QueryRow("SELECT media_path FROM cluster_upload_sessions WHERE upload_id=?", operation).Scan(&ledgerPath); err != nil || ledgerPath != object.Path {
		t.Fatal("rename lost accounting ledger", ledgerPath, err)
	}
	if err := pool.CompleteOwnedRename(ctx, relID, target, "music/AnotherOwnedQuota", renameID, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("operation reused for another move", err)
	}
	if err := sqlDB.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", row.ID).Scan(&used, &reserved); err != nil || used != 3*store.GiB || reserved != 0 {
		t.Fatal("rename changed quota", used, reserved, err)
	}
	deleteID := "f0" + strings.Repeat("8", 30)
	deletion := store.DeleteOperation{ID: deleteID, State: "pending", Items: []store.DeleteItem{{Path: object.Path, Slot: "0", OwnedID: object.ID, Bytes: 3 * store.GiB}}}
	if err = pool.PrepareOwnedDelete(ctx, relID, deletion, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err = db.Media().CommitDelete(ctx, deleteID, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("standalone deletion bypassed quota", err)
	}
	for range 16 {
		group.Go(func() {
			if err := pool.CommitOwnedDelete(ctx, deleteID, 5*store.GiB, store.NodeAudit{RequestID: "owned-quota-delete"}); err != nil {
				t.Error(err)
			}
		})
	}
	group.Wait()
	if err = sqlDB.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", row.ID).Scan(&used, &reserved); err != nil || used != 0 || reserved != 0 {
		t.Fatal("owned deletion duplicate refund", used, reserved, err)
	}
	if err = db.Media().ForgetDelete(ctx, deleteID); err != nil {
		t.Fatal(err)
	}
}
