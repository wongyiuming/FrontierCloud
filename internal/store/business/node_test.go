package business_test

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestNodePairConsumptionNonceRevocationAndHeartbeatTransactions(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	repo := db.Nodes()
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	initial := store.NodeIdentity{ID: strings.Repeat("1", 32), Role: "Standalone", PrivateKey: "encrypted-test-fixture", CreatedAt: time.Now().Unix()}
	row, err := repo.InitializeIdentity(ctx, initial)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			sqlDB.Exec("DELETE FROM "+table+" WHERE member_id=?", strings.Repeat("4", 32))
		}
		sqlDB.Exec("DELETE FROM node_request_nonces WHERE relationship_id IN (?,?)", strings.Repeat("2", 32), strings.Repeat("3", 32))
		sqlDB.Exec("DELETE FROM node_relationships WHERE peer_id=?", strings.Repeat("4", 32))
		sqlDB.Exec("DELETE FROM node_pair_packages WHERE nonce=?", strings.Repeat("5", 32))
		sqlDB.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", row.Role, row.Endpoint)
	})
	if _, err = sqlDB.Exec("UPDATE node_identity SET `role`='Follower',endpoint='https://follower.test' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	now := time.Now().Unix()
	p := store.PairPackage{Nonce: strings.Repeat("5", 32), TokenHash: strings.Repeat("6", 64), ExpiresAt: now + 300}
	issued, err := repo.IssuePair(ctx, p, now, store.NodeAudit{Actor: "test"})
	if err != nil || issued.Role != "Follower" {
		t.Fatal("issue", err)
	}
	if err = repo.PairAvailable(ctx, p, now); err != nil {
		t.Fatal(err)
	}
	v := store.Relationship{ID: strings.Repeat("2", 32), PeerID: strings.Repeat("4", 32), Endpoint: "https://master.test", PublicKey: strings.Repeat("A", 43), Credential: "encrypted-credential", Direction: "upstream", Mode: "Relay", State: "pending", Protocol: 2, PeerVersion: "2.0.0rc0", CreatedAt: now}
	var workers sync.WaitGroup
	var successes atomic.Int32
	failures := make(chan error, 16)
	for range 16 {
		workers.Go(func() {
			err := repo.ConsumePair(ctx, p, v, now, store.NodeAudit{Actor: v.PeerID})
			if err == nil {
				successes.Add(1)
			} else if !errors.Is(err, store.ErrNodeState) {
				failures <- err
			}
		})
	}
	workers.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	if successes.Load() != 1 {
		t.Fatalf("consumed %d times", successes.Load())
	}
	if err = repo.PairAvailable(ctx, p, now); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("consumed pair reusable", err)
	}
	if _, err = repo.ReserveNodeNonce(ctx, v.ID, strings.Repeat("7", 32), v.Credential, now, false, false); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("pending ordinary auth", err)
	}
	if err = repo.ActivateRelationship(ctx, v.ID, store.NodeAudit{Actor: "test"}); err != nil {
		t.Fatal(err)
	}
	successes.Store(0)
	failures = make(chan error, 16)
	for range 16 {
		workers.Go(func() {
			_, err := repo.ReserveNodeNonce(ctx, v.ID, strings.Repeat("7", 32), v.Credential, now, false, false)
			if err == nil {
				successes.Add(1)
			} else if !errors.Is(err, store.ErrNodeState) {
				failures <- err
			}
		})
	}
	workers.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	if successes.Load() != 1 {
		t.Fatalf("nonce accepted %d times", successes.Load())
	}
	if _, err = repo.ReserveNodeNonce(ctx, v.ID, strings.Repeat("8", 32), "old-credential", now, false, false); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("stale credential", err)
	}
	if err = repo.RecordHeartbeat(ctx, v.ID, true, 9, map[string]any{"protocol": 2}, now); err != nil {
		t.Fatal(err)
	}
	if err = repo.RecordHeartbeat(ctx, v.ID, false, 0, nil, now+30); err != nil {
		t.Fatal(err)
	}
	got, err := repo.Relationship(ctx, v.ID)
	if err != nil || got.Status != "degraded" || got.LastHeartbeat != now || got.Failures != 1 {
		t.Fatalf("degraded %+v %v", got, err)
	}
	if err = repo.RecordHeartbeat(ctx, v.ID, false, 0, nil, now+121); err != nil {
		t.Fatal(err)
	}
	got, _ = repo.Relationship(ctx, v.ID)
	if got.Status != "offline" {
		t.Fatalf("offline %+v", got)
	}
	if err = repo.RecordHeartbeat(ctx, v.ID, true, 3, map[string]any{"protocol": 2}, now+122); err != nil {
		t.Fatal(err)
	}
	got, _ = repo.Relationship(ctx, v.ID)
	if got.Recoveries != 1 || got.Status != "online" || got.RTT != 3 || got.Failures != 0 {
		t.Fatalf("recovery %+v", got)
	}
	if err = repo.SetRelationshipMode(ctx, v.ID, "Direct", false, store.NodeAudit{Actor: "test"}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("Follower selected mode", err)
	}
	if err = repo.SetRelationshipMode(ctx, v.ID, "Direct", true, store.NodeAudit{Actor: v.PeerID}); err != nil {
		t.Fatal(err)
	}
	// A stored media object must fence revocation even when its physical file is
	// temporarily missing. The Master must drain its allocation explicitly.
	objects, err := db.Media().EnsureObjects(ctx, []store.MediaObject{{Path: "music/NodePair/track.mp3", Kind: "audio"}})
	if err != nil {
		t.Fatal(err)
	}
	if err = repo.RevokeRelationship(ctx, v.ID, true, store.NodeAudit{Actor: "test"}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("revoked occupied Follower", err)
	}
	if _, err = sqlDB.Exec("DELETE FROM media_objects WHERE media_id=?", objects["music/NodePair/track.mp3"]); err != nil {
		t.Fatal(err)
	}
	// Other business tests share this disposable database; they may have media
	// objects too. Switch to Master to test its peer-scoped drain fence.
	if _, err = sqlDB.Exec("UPDATE node_identity SET `role`='Master' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	if err = repo.RevokeRelationship(ctx, v.ID, false, store.NodeAudit{Actor: "test"}); err != nil {
		t.Fatal(err)
	}
	if _, err = repo.ReserveNodeNonce(ctx, v.ID, strings.Repeat("8", 32), v.Credential, now, false, false); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("revoked ordinary auth", err)
	}
	if err = repo.ActivateRelationship(ctx, v.ID, store.NodeAudit{Actor: "test"}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("revoked activated", err)
	}
	replacement := v
	replacement.ID = strings.Repeat("3", 32)
	replacement.Direction = "downstream"
	if err = repo.PrepareRelationship(ctx, replacement, store.NodeAudit{Actor: "test"}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("re-pair without acknowledgement", err)
	}
	if err = repo.AcknowledgeRevocation(ctx, v.ID, store.NodeAudit{Actor: "peer"}); err != nil {
		t.Fatal(err)
	}
	if err = repo.PrepareRelationship(ctx, replacement, store.NodeAudit{Actor: "test"}); err != nil {
		t.Fatal(err)
	}
	if _, err = repo.Relationship(ctx, v.ID); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("old relationship retained", err)
	}
	if err = repo.ActivateRelationship(ctx, replacement.ID, store.NodeAudit{Actor: "test"}); err != nil {
		t.Fatal(err)
	}
	if err = repo.SetRelationshipMode(ctx, replacement.ID, "Direct", false, store.NodeAudit{Actor: "test"}); err != nil {
		t.Fatal(err)
	}
	var count int
	if err = sqlDB.QueryRow("SELECT COUNT(*) FROM node_audit WHERE relationship_id=? AND action='pair-consumed'", v.ID).Scan(&count); err != nil || count != 1 {
		t.Fatalf("duplicate audit %d %v", count, err)
	}
}

func TestNodeAuditFailureRollsBackIssuedPair(t *testing.T) {
	db := database(t)
	if db.Backend() != "sqlite" {
		t.Skip("SQLite audit trigger injection")
	}
	ctx := context.Background()
	repo := db.Nodes()
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	if _, err := repo.InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Follower", Endpoint: "https://node.test", PrivateKey: "encrypted-fixture"}); err != nil {
		t.Fatal(err)
	}
	if _, err := sqlDB.Exec("CREATE TRIGGER node_audit_fault BEFORE INSERT ON node_audit BEGIN SELECT RAISE(FAIL,'audit unavailable'); END"); err != nil {
		t.Fatal(err)
	}
	now := time.Now().Unix()
	p := store.PairPackage{Nonce: strings.Repeat("b", 32), TokenHash: strings.Repeat("c", 64), ExpiresAt: now + 300}
	if _, err := repo.IssuePair(ctx, p, now, store.NodeAudit{Actor: "test"}); err == nil {
		t.Fatal("unaudited pair committed")
	}
	if err := repo.PairAvailable(ctx, p, now); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("pair survived rollback", err)
	}
}
