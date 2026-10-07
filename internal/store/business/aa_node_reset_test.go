package business_test

import (
	"context"
	"database/sql"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"strings"
	"testing"
	"time"
)

func TestNodeResetAtomicIdentityRotationTombstonesAndColdHistory(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	raw := db.(interface{ Database() *sql.DB }).Database()
	before, err := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "reset-fixture-original"})
	if err != nil {
		t.Fatal(err)
	}
	peer, relation := strings.Repeat("9", 32), "d9"+strings.Repeat("9", 30)
	next := store.NodeIdentity{ID: "e9" + strings.Repeat("9", 30), Role: "Standalone", PrivateKey: "reset-fixture-next"}
	audit := store.NodeAudit{Actor: "native-reset-fixture"}
	t.Cleanup(func() {
		raw.Exec("DELETE FROM node_relationships WHERE relationship_id=?", relation)
		raw.Exec("DELETE FROM cluster_business_backups WHERE master_id=?", peer)
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			raw.Exec("DELETE FROM "+table+" WHERE member_id IN (?,?,?)", before.ID, peer, next.ID)
		}
		raw.Exec("UPDATE node_identity SET node_id=?,`role`=?,endpoint=?,private_key=? WHERE singleton=1", before.ID, before.Role, before.Endpoint, before.PrivateKey)
		raw.Exec("DELETE FROM node_audit WHERE actor='native-reset-fixture'")
	})
	if _, err := db.Nodes().PromoteIdentity(ctx, store.NodePromotion{Role: "Master", Endpoint: "https://reset.test", Allocation: store.GiB, PhysicalFree: 10 * store.GiB}, audit); err != nil {
		t.Fatal(err)
	}
	value := store.Relationship{ID: relation, PeerID: peer, Endpoint: "https://reset-peer.test", PublicKey: strings.Repeat("A", 43), Credential: "encrypted-retained-revocation", Direction: "downstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: time.Now().Unix()}
	if err := db.Nodes().PrepareRelationship(ctx, value, audit); err != nil {
		t.Fatal(err)
	}
	if err := db.Nodes().ActivateRelationship(ctx, relation, audit); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("INSERT INTO cluster_business_backups(master_id,generation,checksum,size_bytes,chunk_count,state,created_at,updated_at) VALUES (?,7,?,0,0,'ready',1,1)", peer, strings.Repeat("f", 64)); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Nodes().ResetIdentity(ctx, before.ID, "Standalone", next, audit); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("role inspection race ignored", err)
	}
	if _, err := raw.Exec("UPDATE cluster_storage_members SET reserved_bytes=1 WHERE member_id=?", before.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Nodes().ResetIdentity(ctx, before.ID, "Master", next, audit); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("live capacity discarded", err)
	}
	if _, err := raw.Exec("UPDATE cluster_storage_members SET reserved_bytes=0 WHERE member_id=?", before.ID); err != nil {
		t.Fatal(err)
	}
	constraint := "native_reset_audit_fail"
	ddl, drop := "CREATE TRIGGER "+constraint+" BEFORE INSERT ON node_audit WHEN NEW.action='reinitialize' BEGIN SELECT RAISE(ABORT,'injected reset audit failure'); END", "DROP TRIGGER "+constraint
	if db.Backend() == "mysql" {
		ddl = "ALTER TABLE node_audit ADD CONSTRAINT " + constraint + " CHECK (action <> 'reinitialize')"
		drop = "ALTER TABLE node_audit DROP CHECK " + constraint
	}
	if _, err := raw.Exec(ddl); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { raw.Exec(drop) })
	if _, err := db.Nodes().ResetIdentity(ctx, before.ID, "Master", next, audit); err == nil {
		t.Fatal("failed audit committed reset")
	}
	actual, err := db.Nodes().ReadIdentity(ctx)
	if err != nil || actual.ID != before.ID || actual.Role != "Master" || actual.PrivateKey != before.PrivateKey {
		t.Fatal("identity escaped rollback", actual, err)
	}
	old, err := db.Nodes().Relationship(ctx, relation)
	if err != nil || old.State != "active" {
		t.Fatal("tombstone escaped rollback", old, err)
	}
	if _, err := raw.Exec(drop); err != nil {
		t.Fatal(err)
	}
	result, err := db.Nodes().ResetIdentity(ctx, before.ID, "Master", next, audit)
	if err != nil || result.ID != next.ID || result.Role != "Standalone" {
		t.Fatal(result, err)
	}
	old, err = db.Nodes().Relationship(ctx, relation)
	if err != nil || old.State != "revoked" || old.Status != "offline" || old.Credential != value.Credential || old.PublicKey != value.PublicKey {
		t.Fatal("revocation proof erased", old, err)
	}
	var state string
	if err := raw.QueryRow("SELECT state FROM cluster_business_backups WHERE master_id=? AND generation=7", peer).Scan(&state); err != nil || state != "ready" {
		t.Fatal("cold history changed", state, err)
	}
	if _, err := db.Nodes().ResetIdentity(ctx, before.ID, "Master", next, audit); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("old confirmation replay accepted", err)
	}
}
