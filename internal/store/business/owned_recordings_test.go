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

func TestOwnedRecordingQuotaReplayPhysicalDeletionAndUnusedTicketTombstone(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	raw := db.(interface{ Database() *sql.DB }).Database()
	nodes, repo := db.Nodes(), db.Recordings()
	identity, e := nodes.InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "fixture"})
	if e != nil {
		t.Fatal(e)
	}
	if _, e = raw.Exec("UPDATE node_identity SET `role`='Follower',endpoint='https://record-owned.test' WHERE singleton=1"); e != nil {
		t.Fatal(e)
	}
	relID, nonce := "ef"+strings.Repeat("1", 30), "ef"+strings.Repeat("2", 30)
	owner := "ef" + strings.Repeat("3", 30)
	id := "ef" + strings.Repeat("4", 30)
	t.Cleanup(func() {
		raw.Exec("DELETE FROM karaoke_recordings WHERE user_id=?", owner)
		raw.Exec("DELETE FROM karaoke_users WHERE user_id=?", owner)
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			raw.Exec("DELETE FROM "+table+" WHERE member_id=?", identity.ID)
		}
		raw.Exec("DELETE FROM node_relationships WHERE relationship_id=?", relID)
		raw.Exec("DELETE FROM node_pair_packages WHERE nonce=?", nonce)
		raw.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", identity.Role, identity.Endpoint)
	})
	now := time.Now().Unix()
	pack := store.PairPackage{Nonce: nonce, TokenHash: strings.Repeat("e", 64), ExpiresAt: now + 300}
	if _, e = nodes.IssuePair(ctx, pack, now, store.NodeAudit{}); e != nil {
		t.Fatal(e)
	}
	rel := store.Relationship{ID: relID, PeerID: "ef" + strings.Repeat("5", 30), Endpoint: "https://master.test", PublicKey: strings.Repeat("A", 43), Credential: "fixture-encrypted", Direction: "upstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: now}
	if e = nodes.ConsumePair(ctx, pack, rel, now, store.NodeAudit{}); e != nil {
		t.Fatal(e)
	}
	if e = nodes.ActivateRelationship(ctx, relID, store.NodeAudit{}); e != nil {
		t.Fatal(e)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled = true
	cfg.Storage.Allocation = 5 * store.GiB
	if e = db.Pool().AcceptFollowerConfiguration(ctx, relID, cfg, 5*store.GiB); e != nil {
		t.Fatal(e)
	}
	v := store.Recording{ID: id, UserID: owner, Filename: "录音.webm", ContentType: "audio/webm", Bytes: 100}
	var wg sync.WaitGroup
	for range 16 {
		wg.Go(func() {
			if e := repo.ReserveOwnedRecording(ctx, relID, v, 5*store.GiB, store.NodeAudit{}); e != nil {
				t.Error(e)
			}
		})
	}
	wg.Wait()
	receipt := store.RecordingReceipt{ID: id, Bytes: 100, SHA256: strings.Repeat("d", 64)}
	for range 16 {
		wg.Go(func() {
			if e := repo.CompleteOwnedRecording(ctx, relID, receipt, 5*store.GiB, store.NodeAudit{}); e != nil {
				t.Error(e)
			}
		})
	}
	wg.Wait()
	var used, reserved int64
	if e = raw.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", identity.ID).Scan(&used, &reserved); e != nil || used != 100 || reserved != 0 {
		t.Fatal(used, reserved, e)
	}
	if e = repo.CompleteOwnedRecordingDeletion(ctx, relID, owner, id, 5*store.GiB, store.NodeAudit{}); !errors.Is(e, store.ErrRecordingState) {
		t.Fatal("delete missing durable intent", e)
	}
	if _, e = repo.StageOwnedRecordingDeletion(ctx, relID, strings.Repeat("9", 32), id); !errors.Is(e, store.ErrRecordingState) {
		t.Fatal("cross-owner deletion", e)
	}
	if _, e = repo.StageOwnedRecordingDeletion(ctx, relID, owner, id); e != nil {
		t.Fatal(e)
	}
	for range 16 {
		wg.Go(func() {
			if e := repo.CompleteOwnedRecordingDeletion(ctx, relID, owner, id, 5*store.GiB, store.NodeAudit{}); e != nil {
				t.Error(e)
			}
		})
	}
	wg.Wait()
	if e = repo.ReserveOwnedRecording(ctx, relID, v, 5*store.GiB, store.NodeAudit{}); !errors.Is(e, store.ErrRecordingState) {
		t.Fatal("deleted ticket replay", e)
	}
	unused := v
	unused.ID = "ef" + strings.Repeat("6", 30)
	if _, e = repo.StageOwnedRecordingDeletion(ctx, relID, owner, unused.ID); e != nil {
		t.Fatal(e)
	}
	if e = repo.ReserveOwnedRecording(ctx, relID, unused, 5*store.GiB, store.NodeAudit{}); !errors.Is(e, store.ErrRecordingState) {
		t.Fatal("cancelled unused ticket replay", e)
	}
	if e = raw.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", identity.ID).Scan(&used, &reserved); e != nil || used != 0 || reserved != 0 {
		t.Fatal("refund replay", used, reserved, e)
	}
	if e = repo.StageOwnedUserDeletion(ctx, relID, owner); e != nil {
		t.Fatal(e)
	}
	newTicket := v
	newTicket.ID = "ef" + strings.Repeat("7", 30)
	if e = repo.ReserveOwnedRecording(ctx, relID, newTicket, 5*store.GiB, store.NodeAudit{}); !errors.Is(e, store.ErrRecordingState) {
		t.Fatal("deleted owner accepted a new old capability", e)
	}
	var blockers int
	if e = raw.QueryRow("SELECT COUNT(*) FROM karaoke_recordings WHERE storage_member_id=? AND state IN ('pending','ready','deleting')", identity.ID).Scan(&blockers); e != nil || blockers != 0 {
		t.Fatal("recording tombstones remain revocation blockers", blockers, e)
	}
	// The real-driver suite shares a disposable DB. Earlier standalone media
	// objects legitimately block Follower revocation; do not delete those rows
	// just to make this ownership-tombstone test pass.
	if db.Backend() == "sqlite" {
		if e = nodes.RevokeRelationship(ctx, relID, false, store.NodeAudit{}); e != nil {
			t.Fatal("empty Follower cannot revoke", e)
		}
	}
}
