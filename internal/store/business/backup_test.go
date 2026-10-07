package business_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func backupFixture(t *testing.T) (store.Store, *sql.DB, store.Relationship) {
	t.Helper()
	db := database(t)
	raw := db.(interface{ Database() *sql.DB }).Database()
	ctx := context.Background()
	identity, err := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "backup-fixture"})
	if err != nil {
		t.Fatal(err)
	}
	id := func(s string) string {
		hash := sha256.Sum256([]byte(t.Name() + s))
		return hex.EncodeToString(hash[:16])
	}
	rel := store.Relationship{ID: id("relationship"), PeerID: id("master"), Endpoint: "https://backup-master.test", PublicKey: strings.Repeat("A", 43), Credential: "fixture-encrypted", Direction: "upstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: time.Now().Unix()}
	nonce := id("nonce")
	t.Cleanup(func() {
		raw.Exec("DELETE FROM cluster_business_backup_chunks WHERE master_id=?", rel.PeerID)
		raw.Exec("DELETE FROM cluster_business_backups WHERE master_id=?", rel.PeerID)
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			raw.Exec("DELETE FROM "+table+" WHERE member_id=?", identity.ID)
		}
		raw.Exec("DELETE FROM node_audit WHERE relationship_id=?", rel.ID)
		raw.Exec("DELETE FROM node_relationships WHERE relationship_id=?", rel.ID)
		raw.Exec("DELETE FROM node_pair_packages WHERE nonce=?", nonce)
		raw.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", identity.Role, identity.Endpoint)
	})
	if _, err := raw.Exec("UPDATE node_identity SET `role`='Follower',endpoint='https://backup-follower.test' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	now := time.Now().Unix()
	pack := store.PairPackage{Nonce: nonce, TokenHash: strings.Repeat("e", 64), ExpiresAt: now + 300}
	if _, err := db.Nodes().IssuePair(ctx, pack, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := db.Nodes().ConsumePair(ctx, pack, rel, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := db.Nodes().ActivateRelationship(ctx, rel.ID, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	rel.State = "active"
	cfg := store.ResourceConfiguration{}
	cfg.Backup.Enabled = true
	if err := db.Pool().AcceptFollowerConfiguration(ctx, rel.ID, cfg, 5*store.GiB); err != nil {
		t.Fatal(err)
	}
	return db, raw, rel
}

func backupChecksum(payload []byte) string {
	hash := sha256.Sum256(payload)
	return hex.EncodeToString(hash[:])
}

func TestBusinessBackupBoundedChunksChecksumReplayRetentionAndAbort(t *testing.T) {
	db, raw, rel := backupFixture(t)
	repo, ctx := db.Backups(), context.Background()
	payload := bytes.Repeat([]byte("\x00binary备份\xff"), 20_000)
	first, second := payload[:store.MaxBackupChunk], payload[store.MaxBackupChunk:]
	if len(second) > store.MaxBackupChunk {
		t.Fatal("invalid test payload")
	}
	if err := repo.BeginBackup(ctx, rel.ID, 100, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for range 16 {
		wg.Go(func() {
			if err := repo.AppendBackup(ctx, rel.ID, 100, 1, second); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if err := repo.AppendBackup(ctx, rel.ID, 100, 1, []byte("different")); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("duplicate replaced", err)
	}
	if _, err := repo.CommitBackup(ctx, rel.ID, 100, backupChecksum(payload), store.NodeAudit{}); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("gap committed", err)
	}
	if err := repo.AppendBackup(ctx, rel.ID, 100, 0, first); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.CommitBackup(ctx, rel.ID, 100, strings.Repeat("0", 64), store.NodeAudit{}); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("checksum mismatch committed", err)
	}
	for range 16 {
		wg.Go(func() {
			manifest, err := repo.CommitBackup(ctx, rel.ID, 100, backupChecksum(payload), store.NodeAudit{RequestID: "backup-100"})
			if err != nil || manifest.State != "ready" || manifest.Bytes != int64(len(payload)) || manifest.Chunks != 2 {
				t.Error(manifest, err)
			}
		})
	}
	wg.Wait()
	var count int
	if err := raw.QueryRow("SELECT COUNT(*) FROM node_audit WHERE relationship_id=? AND action='backup-ready'", rel.ID).Scan(&count); err != nil || count != 1 {
		t.Fatal("ready audit replay", count, err)
	}
	if err := repo.BeginBackup(ctx, rel.ID, 100, store.NodeAudit{}); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("ready generation erased", err)
	}
	if err := repo.AppendBackup(ctx, rel.ID, 100, 2, []byte("late")); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("ready generation appended", err)
	}
	// Finish generations out of order; the member's latest pointer cannot regress.
	for _, generation := range []int64{102, 101} {
		if err := repo.BeginBackup(ctx, rel.ID, generation, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		if err := repo.AppendBackup(ctx, rel.ID, generation, 0, payload[:100]); err != nil {
			t.Fatal(err)
		}
		if _, err := repo.CommitBackup(ctx, rel.ID, generation, backupChecksum(payload[:100]), store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
	}
	var latest int64
	if err := raw.QueryRow("SELECT generation FROM cluster_backup_members WHERE member_id=(SELECT node_id FROM node_identity WHERE singleton=1)").Scan(&latest); err != nil || latest != 102 {
		t.Fatal("latest regressed", latest, err)
	}
	if err := raw.QueryRow("SELECT COUNT(*) FROM cluster_business_backups WHERE master_id=? AND state='ready'", rel.PeerID).Scan(&count); err != nil || count != 2 {
		t.Fatal("retention", count, err)
	}
	if err := raw.QueryRow("SELECT COUNT(*) FROM cluster_business_backup_chunks WHERE master_id=? AND generation=100", rel.PeerID).Scan(&count); err != nil || count != 0 {
		t.Fatal("stale chunks not removed", count, err)
	}
	for _, generation := range []int64{103, 104} {
		if err := repo.BeginBackup(ctx, rel.ID, generation, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		if err := repo.AppendBackup(ctx, rel.ID, generation, 0, []byte("partial")); err != nil {
			t.Fatal(err)
		}
	}
	if err := repo.AppendBackup(ctx, rel.ID, 104, 1, nil); err != nil {
		t.Fatal("empty chunk became SQL NULL", err)
	}
	result, err := repo.AbortBackups(ctx, rel.ID, 999, store.NodeAudit{})
	if err != nil || result.Status != "failed" || result.Generation != 999 || result.AbortedGenerations != 2 {
		t.Fatal(result, err)
	}
	result, err = repo.AbortBackups(ctx, rel.ID, 999, store.NodeAudit{})
	if err != nil || result.Status != "clean" || result.AbortedGenerations != 0 {
		t.Fatal("abort replay", result, err)
	}
	if err := raw.QueryRow("SELECT COUNT(*) FROM cluster_business_backup_chunks WHERE master_id=? AND generation IN (103,104)", rel.PeerID).Scan(&count); err != nil || count != 0 {
		t.Fatal("partial chunks retained", count, err)
	}
	if err := raw.QueryRow("SELECT COUNT(*) FROM cluster_business_backups WHERE master_id=? AND state='ready'", rel.PeerID).Scan(&count); err != nil || count != 2 {
		t.Fatal("abort damaged ready backups", count, err)
	}
	for _, bad := range []struct {
		generation  int64
		index, size int
	}{{0, 0, 1}, {1, -1, 1}, {1, store.MaxBackupChunkIndex + 1, 1}, {1, 0, store.MaxBackupChunk + 1}} {
		if err := repo.AppendBackup(ctx, rel.ID, bad.generation, bad.index, make([]byte, bad.size)); !errors.Is(err, store.ErrBackupState) {
			t.Fatal("invalid chunk accepted", bad, err)
		}
	}
	if err := repo.BeginBackup(ctx, rel.ID, 105, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("UPDATE node_relationships SET state='revoked' WHERE relationship_id=?", rel.ID); err != nil {
		t.Fatal(err)
	}
	if err := repo.AppendBackup(ctx, rel.ID, 105, 0, []byte("late")); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("revoked upstream accepted", err)
	}
	if _, err := repo.CommitBackup(ctx, rel.ID, 105, backupChecksum([]byte("late")), store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("revoked commit", err)
	}
	if _, err := repo.AbortBackups(ctx, rel.ID, 105, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("revoked abort", err)
	}
}

func TestBusinessBackupReadyAuditFailureRollsBackVerifiedGeneration(t *testing.T) {
	db, raw, rel := backupFixture(t)
	ctx, repo := context.Background(), db.Backups()
	data := []byte("backup-audit-rollback")
	if err := repo.BeginBackup(ctx, rel.ID, 200, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := repo.AppendBackup(ctx, rel.ID, 200, 0, data); err != nil {
		t.Fatal(err)
	}
	trigger := "fc_backup_audit_failure"
	ddl := "CREATE TRIGGER " + trigger + " BEFORE INSERT ON node_audit WHEN NEW.action='backup-ready' BEGIN SELECT RAISE(ABORT,'backup audit failed'); END"
	drop := "DROP TRIGGER " + trigger
	if db.Backend() == "mysql" {
		// A disposable MySQL CHECK produces the same transactional audit
		// failure without granting SUPER or weakening binary-log security.
		ddl = "ALTER TABLE node_audit ADD CONSTRAINT " + trigger + " CHECK (action <> 'backup-ready')"
		drop = "ALTER TABLE node_audit DROP CHECK " + trigger
	}
	if _, err := raw.Exec(ddl); err != nil {
		t.Fatal(err)
	}
	injected := true
	t.Cleanup(func() {
		if injected {
			raw.Exec(drop)
		}
	})
	if _, err := repo.CommitBackup(ctx, rel.ID, 200, backupChecksum(data), store.NodeAudit{}); err == nil {
		t.Fatal("failed audit committed")
	}
	var state string
	if err := raw.QueryRow("SELECT state FROM cluster_business_backups WHERE master_id=? AND generation=200", rel.PeerID).Scan(&state); err != nil || state != "receiving" {
		t.Fatal("intent not retained", state, err)
	}
	var count int
	if err := raw.QueryRow("SELECT COUNT(*) FROM cluster_business_backup_chunks WHERE master_id=? AND generation=200", rel.PeerID).Scan(&count); err != nil || count != 1 {
		t.Fatal("retry bytes lost", count, err)
	}
	if _, err := raw.Exec(drop); err != nil {
		t.Fatal(err)
	}
	injected = false
	if _, err := repo.CommitBackup(ctx, rel.ID, 200, backupChecksum(data), store.NodeAudit{}); err != nil {
		t.Fatal("retry failed", err)
	}
}
