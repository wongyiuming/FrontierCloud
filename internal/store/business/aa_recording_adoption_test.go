package business_test

import (
	"context"
	"crypto/ed25519"
	"database/sql"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestRecordingInventoryPinnedAuthorityAtomicAdoptionQuotaAndTombstones(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	raw := db.(interface{ Database() *sql.DB }).Database()
	n, err := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "inventory-fixture"})
	if err != nil {
		t.Fatal(err)
	}
	relID, master, user, id := "ad"+strings.Repeat("1", 30), "ad"+strings.Repeat("2", 30), "ad"+strings.Repeat("3", 30), "ad"+strings.Repeat("4", 30)
	seed := protocol.Encode(make([]byte, 32))
	private := ed25519.NewKeyFromSeed(make([]byte, 32))
	public := protocol.Encode(private.Public().(ed25519.PublicKey))
	now := time.Now().Unix()
	t.Cleanup(func() {
		raw.Exec("DELETE FROM karaoke_recordings WHERE user_id=?", user)
		raw.Exec("DELETE FROM karaoke_users WHERE user_id=?", user)
		raw.Exec("DELETE FROM cluster_storage_members WHERE member_id=?", n.ID)
		raw.Exec("DELETE FROM node_relationships WHERE relationship_id=?", relID)
		raw.Exec("DELETE FROM node_pair_packages WHERE nonce=?", master)
		raw.Exec("DELETE FROM node_audit WHERE actor='recording-inventory-fixture'")
		raw.Exec("UPDATE node_identity SET node_id=?,`role`=?,endpoint=?,private_key=? WHERE singleton=1", n.ID, n.Role, n.Endpoint, n.PrivateKey)
	})
	if _, err = raw.Exec("UPDATE node_identity SET `role`='Follower' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	pack := store.PairPackage{Nonce: master, TokenHash: strings.Repeat("c", 64), ExpiresAt: now + 300}
	if _, err = db.Nodes().IssuePair(ctx, pack, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	rel := store.Relationship{ID: relID, PeerID: master, Endpoint: "https://inventory.test", PublicKey: public, Credential: "sealed-inventory-fixture", Direction: "upstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: now}
	if err = db.Nodes().ConsumePair(ctx, pack, rel, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err = db.Nodes().ActivateRelationship(ctx, relID, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled = true
	cfg.Storage.Allocation = 2 * store.GiB
	if err = db.Pool().AcceptFollowerConfiguration(ctx, relID, cfg, 5*store.GiB); err != nil {
		t.Fatal(err)
	}
	proof := store.RecordingProof{ID: id, UserID: user, Filename: "旧录音.webm", ContentType: "audio/webm", Bytes: 42, SHA256: strings.Repeat("b", 64), CreatedAt: now - 1}
	if _, err = raw.Exec("INSERT INTO karaoke_recordings(recording_id,user_id,storage_member_id,filename,content_type,size_bytes,sha256,state,title,lyrics,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'ready','','[]',?,?)", proof.ID, proof.UserID, n.ID, proof.Filename, proof.ContentType, proof.Bytes, proof.SHA256, proof.CreatedAt, now); err != nil {
		t.Fatal(err)
	}
	if _, err = raw.Exec("UPDATE cluster_storage_members SET used_bytes=42 WHERE member_id=?", n.ID); err != nil {
		t.Fatal(err)
	}
	// Export the exact same ready rows through a current Master/downstream view.
	if _, err = raw.Exec("UPDATE node_identity SET node_id=?,`role`='Master' WHERE singleton=1", master); err != nil {
		t.Fatal(err)
	}
	if _, err = raw.Exec("UPDATE node_relationships SET peer_id=?,direction='downstream' WHERE relationship_id=?", n.ID, relID); err != nil {
		t.Fatal(err)
	}
	if _, err = raw.Exec("UPDATE cluster_storage_members SET relationship_id=?,transport='Direct' WHERE member_id=?", relID, n.ID); err != nil {
		t.Fatal(err)
	}
	inventory, err := db.Maintenance().ExportRecordingInventory(ctx, relID, now)
	if err != nil || len(inventory.Recordings) != 1 || inventory.Recordings[0] != proof {
		t.Fatal(inventory, err)
	}
	if _, err = raw.Exec("UPDATE node_identity SET node_id=?,`role`='Follower' WHERE singleton=1", n.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = raw.Exec("UPDATE node_relationships SET peer_id=?,direction='upstream' WHERE relationship_id=?", master, relID); err != nil {
		t.Fatal(err)
	}
	if _, err = raw.Exec("UPDATE cluster_storage_members SET relationship_id=NULL,transport='Local' WHERE member_id=?", n.ID); err != nil {
		t.Fatal(err)
	}
	raw.Exec("DELETE FROM karaoke_recordings WHERE recording_id=?", id)
	payload, err := inventory.CanonicalPayload()
	if err != nil {
		t.Fatal(err)
	}
	signature, err := protocol.Sign(seed, payload)
	if err != nil {
		t.Fatal(err)
	}
	signed := store.SignedRecordingInventory{Payload: inventory, Signature: signature}
	audit := store.NodeAudit{Actor: "recording-inventory-fixture"}
	bad := signed
	bad.Payload.MasterID = n.ID
	if _, err = db.Maintenance().AdoptOwnedRecordings(ctx, bad, n.ID, audit); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("tampered authority", err)
	}
	if _, err = raw.Exec("UPDATE cluster_storage_members SET used_bytes=43 WHERE member_id=?", n.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = db.Maintenance().AdoptOwnedRecordings(ctx, signed, n.ID, audit); !errors.Is(err, store.ErrRecordingState) {
		t.Fatal("unexplained quota", err)
	}
	raw.Exec("UPDATE cluster_storage_members SET used_bytes=42 WHERE member_id=?", n.ID)
	constraint := "recording_inventory_audit_fail"
	ddl, drop := "CREATE TRIGGER "+constraint+" BEFORE INSERT ON node_audit WHEN NEW.action='recording-owned-adopted' BEGIN SELECT RAISE(ABORT,'injected'); END", "DROP TRIGGER "+constraint
	if db.Backend() == "mysql" {
		ddl = "ALTER TABLE node_audit ADD CONSTRAINT " + constraint + " CHECK (action <> 'recording-owned-adopted')"
		drop = "ALTER TABLE node_audit DROP CHECK " + constraint
	}
	if _, err = raw.Exec(ddl); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { raw.Exec(drop) })
	if count, err := db.Maintenance().AdoptOwnedRecordings(ctx, signed, n.ID, audit); err == nil || count != 0 {
		t.Fatal("unaudited adoption", count, err)
	}
	if row, err := db.Recordings().Recording(ctx, id); err != nil || row != nil {
		t.Fatal("row escaped rollback", row, err)
	}
	if _, err = raw.Exec(drop); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		want := 1 - i
		if count, err := db.Maintenance().AdoptOwnedRecordings(ctx, signed, n.ID, audit); err != nil || count != want {
			t.Fatal(count, err)
		}
	}
	var used, reserved int64
	raw.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", n.ID).Scan(&used, &reserved)
	if used != 42 || reserved != 0 {
		t.Fatal("quota changed", used, reserved)
	}
	raw.Exec("UPDATE karaoke_recordings SET state='deleted' WHERE recording_id=?", id)
	if _, err = db.Maintenance().AdoptOwnedRecordings(ctx, signed, n.ID, audit); !errors.Is(err, store.ErrRecordingState) {
		t.Fatal("tombstone recreated", err)
	}
	raw.Exec("UPDATE karaoke_recordings SET state='ready' WHERE recording_id=?", id)
	raw.Exec("UPDATE node_relationships SET state='revoked' WHERE relationship_id=?", relID)
	if _, err = db.Maintenance().AdoptOwnedRecordings(ctx, signed, n.ID, audit); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("revoked pinned key accepted", err)
	}
}
