package business_test

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func hashPath(value string) string {
	digest := sha256.Sum256([]byte(value))
	return fmt.Sprintf("%x", digest[:])
}

func TestMasterPromotionStableIdentityAllocationAndFollowerCapacity(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	nodes, pool := db.Nodes(), db.Pool()
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	row, err := nodes.InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("9", 32), Role: "Standalone", PrivateKey: "encrypted-test-fixture", CreatedAt: time.Now().Unix()})
	if err != nil {
		t.Fatal(err)
	}
	path := "music/GoPool/track.mp3"
	objects, err := db.Media().EnsureObjects(ctx, []store.MediaObject{{Path: path, Kind: "audio"}})
	if err != nil {
		t.Fatal(err)
	}
	id := objects[path]
	memberID := strings.Repeat("d", 32)
	relationID := strings.Repeat("e", 32)
	t.Cleanup(func() {
		sqlDB.Exec("DELETE FROM global_media_objects WHERE path_locator=?", hashPath(path))
		sqlDB.Exec("DELETE FROM media_objects WHERE media_id=?", id)
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			sqlDB.Exec("DELETE FROM "+table+" WHERE member_id IN (?,?)", row.ID, memberID)
		}
		sqlDB.Exec("DELETE FROM node_relationships WHERE relationship_id=?", relationID)
		sqlDB.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", row.Role, row.Endpoint)
	})
	p := store.NodePromotion{Role: "Master", Endpoint: "https://master.test", Allocation: store.GiB, PhysicalUsed: 7, PhysicalFree: 10 * store.GiB, Media: []store.LocalMedia{{MediaObject: store.MediaObject{ID: id, Path: path, Kind: "audio"}, Bytes: 7, ETag: `"etag"`, CreatedAt: time.Now().Unix(), UpdatedAt: time.Now().Unix()}}}
	tooSmall := p
	tooSmall.Allocation = store.GiB - 1
	if _, err = nodes.PromoteIdentity(ctx, tooSmall, store.NodeAudit{Actor: "admin"}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("small allocation", err)
	}
	promoted, err := nodes.PromoteIdentity(ctx, p, store.NodeAudit{Actor: "admin"})
	if err != nil || promoted.Role != "Master" || promoted.ID != row.ID {
		t.Fatal("promotion", promoted, err)
	}
	if _, err = nodes.PromoteIdentity(ctx, p, store.NodeAudit{Actor: "admin"}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("fixed role changed", err)
	}
	if err = pool.AdoptMasterLocal(ctx, 7, 10*store.GiB, p.Media); err != nil {
		t.Fatal("adoption replay", err)
	}
	var objectID, globalID string
	var count int
	if err = sqlDB.QueryRow("SELECT media_id,object_id FROM global_media_objects WHERE path_locator=?", hashPath(path)).Scan(&globalID, &objectID); err != nil || globalID != id || objectID != id {
		t.Fatal("legacy identity replaced", err)
	}
	if err = sqlDB.QueryRow("SELECT COUNT(*) FROM global_media_objects WHERE path_locator=?", hashPath(path)).Scan(&count); err != nil || count != 1 {
		t.Fatal("duplicate local adoption", count, err)
	}
	config := store.ResourceConfiguration{}
	config.Storage.Enabled = true
	config.Storage.Allocation = 2 * store.GiB
	if err = pool.ConfigureMember(ctx, row.ID, config, store.NodeAudit{Actor: "admin"}); err != nil {
		t.Fatal(err)
	}
	config.Storage.Enabled = false
	if err = pool.ConfigureMember(ctx, row.ID, config, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("disabled Master Local", err)
	}
	v := store.Relationship{ID: relationID, PeerID: memberID, Endpoint: "https://follower.test", PublicKey: strings.Repeat("A", 43), Credential: "encrypted-credential", Direction: "downstream", Mode: "Relay", State: "pending", Protocol: 2, PeerVersion: "2.0.0rc0", CreatedAt: time.Now().Unix()}
	if err = nodes.PrepareRelationship(ctx, v, store.NodeAudit{Actor: "admin"}); err != nil {
		t.Fatal(err)
	}
	if err = nodes.ActivateRelationship(ctx, v.ID, store.NodeAudit{Actor: "admin"}); err != nil {
		t.Fatal(err)
	}
	config.Storage.Enabled = true
	config.Storage.Allocation = 2 * store.GiB
	config.Compute.Enabled = true
	config.Compute.Slots = 32
	config.Backup.Enabled = true
	if err = pool.ConfigureMember(ctx, memberID, config, store.NodeAudit{Actor: "admin"}); err != nil {
		t.Fatal(err)
	}
	desired, err := pool.MemberConfiguration(ctx, memberID)
	if err != nil || !desired.Storage.Enabled || !desired.Backup.Enabled || desired.Compute.Enabled || desired.Compute.Slots != 0 {
		t.Fatalf("retired compute desired %+v %v", desired, err)
	}
	if err = nodes.RecordHeartbeat(ctx, v.ID, true, 8, map[string]any{"storage": map[string]any{"physical_free_bytes": int64(3 * store.GiB), "physical_total_bytes": int64(8 * store.GiB)}}, time.Now().Unix()); err != nil {
		t.Fatal(err)
	}
	members, err := pool.Members(ctx)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, member := range members {
		if member.ID == memberID {
			found = true
			if member.Health != "online" || member.Writable != 1 || member.Available != 2*store.GiB || member.PhysicalTotal != 8*store.GiB || member.Compute["enabled"] != 0 {
				t.Fatalf("capacity %+v", member)
			}
		}
	}
	if !found {
		t.Fatal("no Follower pool member")
	}
	if _, err = sqlDB.Exec("UPDATE cluster_storage_members SET used_bytes=?,reserved_bytes=? WHERE member_id=?", store.GiB, store.GiB, memberID); err != nil {
		t.Fatal(err)
	}
	config.Storage.Allocation = store.GiB
	if err = pool.ConfigureMember(ctx, memberID, config, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("shrunk into live reservation", err)
	}
	if err = nodes.RecordHeartbeat(ctx, v.ID, false, 0, nil, time.Now().Unix()+121); err != nil {
		t.Fatal(err)
	}
	members, err = pool.Members(ctx)
	if err != nil {
		t.Fatal(err)
	}
	for _, member := range members {
		if member.ID == memberID && (member.Health != "offline" || member.Available != 0 || member.OfflineStored != store.GiB) {
			t.Fatalf("offline storage %+v", member)
		}
	}
}

func TestFollowerConfigurationPreservesReservationsAndBackupReady(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	nodes, pool := db.Nodes(), db.Pool()
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	row, err := nodes.InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "encrypted-fixture"})
	if err != nil {
		t.Fatal(err)
	}
	// Other real-driver tests share this disposable DB's media tables. Install a
	// fixed-role test fixture; the promotion emptiness gate has a separate test.
	if _, err = sqlDB.Exec("UPDATE node_identity SET `role`='Follower',endpoint='https://follower.test' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			sqlDB.Exec("DELETE FROM "+table+" WHERE member_id=?", row.ID)
		}
		sqlDB.Exec("DELETE FROM node_relationships WHERE relationship_id=?", strings.Repeat("d", 32))
		sqlDB.Exec("DELETE FROM node_pair_packages WHERE nonce=?", strings.Repeat("b", 32))
		sqlDB.Exec("DELETE FROM cluster_business_backups WHERE master_id=? AND generation=5", strings.Repeat("e", 32))
		sqlDB.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", row.Role, row.Endpoint)
	})
	now := time.Now().Unix()
	p := store.PairPackage{Nonce: strings.Repeat("b", 32), TokenHash: strings.Repeat("c", 64), ExpiresAt: now + 300}
	if _, err = nodes.IssuePair(ctx, p, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	v := store.Relationship{ID: strings.Repeat("d", 32), PeerID: strings.Repeat("e", 32), Endpoint: "https://master.test", PublicKey: strings.Repeat("A", 43), Credential: "encrypted-credential", Direction: "upstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: now}
	if err = nodes.ConsumePair(ctx, p, v, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err = nodes.ActivateRelationship(ctx, v.ID, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	c := store.ResourceConfiguration{}
	c.Storage.Enabled = true
	c.Storage.Allocation = 3 * store.GiB
	c.Backup.Enabled = true
	c.Compute.Enabled = true
	c.Compute.Slots = 8
	if err = pool.AcceptFollowerConfiguration(ctx, v.ID, c, 10*store.GiB); err != nil {
		t.Fatal(err)
	}
	if _, err = sqlDB.Exec("UPDATE cluster_storage_members SET used_bytes=?,reserved_bytes=? WHERE member_id=?", store.GiB, store.GiB, row.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = sqlDB.Exec("UPDATE cluster_backup_members SET state='ready',generation=5,last_success=? WHERE member_id=?", now, row.ID); err != nil {
		t.Fatal(err)
	}
	if err = pool.AcceptFollowerConfiguration(ctx, v.ID, c, 9*store.GiB); err != nil {
		t.Fatal(err)
	}
	if _, err = sqlDB.Exec("INSERT INTO cluster_business_backups(master_id,generation,checksum,size_bytes,chunk_count,state,created_at,updated_at) VALUES (?,5,?,123,1,'ready',?,?)", v.PeerID, strings.Repeat("f", 64), now, now); err != nil {
		t.Fatal(err)
	}
	summary, err := pool.FollowerSummary(ctx, 9*store.GiB, 20*store.GiB)
	if err != nil {
		t.Fatal(err)
	}
	storage := summary["storage"].(map[string]any)
	backup := summary["backup"].(map[string]any)
	if storage["reserved_bytes"] != store.GiB || storage["used_bytes"] != store.GiB || backup["state"] != "ready" || backup["generation"] != int64(5) || backup["last_size_bytes"] != int64(123) || backup["recovery_points"] != 1 {
		t.Fatalf("heartbeat erased state %+v", summary)
	}
	c.Storage.Allocation = store.GiB
	if err = pool.AcceptFollowerConfiguration(ctx, v.ID, c, 9*store.GiB); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("Follower reservation lost", err)
	}
}

func TestPromotionAuditFailureRollsBackRoleCatalogAndAllocation(t *testing.T) {
	db := database(t)
	if db.Backend() != "sqlite" {
		t.Skip("SQLite audit fault injection")
	}
	ctx := context.Background()
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	nodes := db.Nodes()
	if _, err := nodes.InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "encrypted-fixture"}); err != nil {
		t.Fatal(err)
	}
	if _, err := sqlDB.Exec("CREATE TRIGGER promotion_audit_fault BEFORE INSERT ON node_audit BEGIN SELECT RAISE(FAIL,'audit unavailable'); END"); err != nil {
		t.Fatal(err)
	}
	p := store.NodePromotion{Role: "Master", Endpoint: "https://master.test", Allocation: store.GiB, PhysicalFree: 10 * store.GiB, PhysicalUsed: 7, Media: []store.LocalMedia{{MediaObject: store.MediaObject{Path: "music/Atomic/track.mp3", Kind: "audio"}, Bytes: 7, ETag: `"etag"`}}}
	if _, err := nodes.PromoteIdentity(ctx, p, store.NodeAudit{Actor: "admin"}); err == nil {
		t.Fatal("unaudited promotion committed")
	}
	row, err := nodes.ReadIdentity(ctx)
	if err != nil || row.Role != "Standalone" {
		t.Fatal("role survived rollback", err)
	}
	for _, table := range []string{"cluster_storage_members", "global_media_objects", "media_objects"} {
		var count int
		if err = sqlDB.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("partial %s %d %v", table, count, err)
		}
	}
}
