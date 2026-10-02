package business_test

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestOwnedAdoptionAtomicCurrentRelationshipCompleteSetAndReplay(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	raw := db.(interface{ Database() *sql.DB }).Database()
	n, err := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "adoption-original"})
	if err != nil {
		t.Fatal(err)
	}
	rel, peer := "a8"+strings.Repeat("8", 30), "b8"+strings.Repeat("8", 30)
	audit := store.NodeAudit{Actor: "offline-adoption-fixture"}
	name := "music/" + t.Name() + "/legacy.mp3"
	var objectID string
	t.Cleanup(func() {
		raw.Exec("DELETE FROM cluster_upload_sessions WHERE media_id=?", objectID)
		raw.Exec("DELETE FROM media_objects WHERE media_id=?", objectID)
		raw.Exec("DELETE FROM node_relationships WHERE relationship_id=?", rel)
		raw.Exec("DELETE FROM cluster_storage_members WHERE member_id=?", n.ID)
		raw.Exec("DELETE FROM node_pair_packages WHERE nonce=?", peer)
		raw.Exec("DELETE FROM node_audit WHERE actor=?", audit.Actor)
		raw.Exec("UPDATE node_identity SET node_id=?,`role`=?,endpoint=?,private_key=? WHERE singleton=1", n.ID, n.Role, n.Endpoint, n.PrivateKey)
	})
	if _, err = db.Nodes().PromoteIdentity(ctx, store.NodePromotion{Role: "Follower", Endpoint: "https://adoption.test"}, audit); err != nil {
		t.Fatal(err)
	}
	now := time.Now().Unix()
	p := store.PairPackage{Nonce: peer, TokenHash: strings.Repeat("c", 64), ExpiresAt: now + 300}
	if _, err = db.Nodes().IssuePair(ctx, p, now, audit); err != nil {
		t.Fatal(err)
	}
	relation := store.Relationship{ID: rel, PeerID: peer, Endpoint: "https://adoption-master.test", PublicKey: strings.Repeat("A", 43), Credential: "sealed-fixture", Direction: "upstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: now}
	if err = db.Nodes().ConsumePair(ctx, p, relation, now, audit); err != nil {
		t.Fatal(err)
	}
	if err = db.Nodes().ActivateRelationship(ctx, rel, audit); err != nil {
		t.Fatal(err)
	}
	configuration := store.ResourceConfiguration{}
	configuration.Storage.Enabled = true
	configuration.Storage.Allocation = 2 * store.GiB
	if err = db.Pool().AcceptFollowerConfiguration(ctx, rel, configuration, 10*store.GiB); err != nil {
		t.Fatal(err)
	}
	ids, err := db.Media().EnsureObjects(ctx, []store.MediaObject{{Path: name, Kind: "audio"}})
	if err != nil {
		t.Fatal(err)
	}
	objectID = ids[name]
	proof := store.LocalMedia{MediaObject: store.MediaObject{ID: objectID, Path: name, Kind: "audio"}, Bytes: 42, ETag: `"` + strings.Repeat("d", 64) + `"`}
	if _, err = raw.Exec("UPDATE cluster_storage_members SET used_bytes=42 WHERE member_id=?", n.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = db.Maintenance().AdoptOwnedStorage(ctx, rel, n.ID, nil, audit); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("partial inventory accepted", err)
	}
	if _, err = db.Maintenance().AdoptOwnedStorage(ctx, rel, peer, []store.LocalMedia{proof}, audit); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("stale identity accepted", err)
	}
	constraint := "native_adoption_audit_fail"
	ddl, drop := "CREATE TRIGGER "+constraint+" BEFORE INSERT ON node_audit WHEN NEW.action='storage-owned-adopted' BEGIN SELECT RAISE(ABORT,'injected'); END", "DROP TRIGGER "+constraint
	if db.Backend() == "mysql" {
		ddl = "ALTER TABLE node_audit ADD CONSTRAINT " + constraint + " CHECK (action <> 'storage-owned-adopted')"
		drop = "ALTER TABLE node_audit DROP CHECK " + constraint
	}
	if _, err = raw.Exec(ddl); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { raw.Exec(drop) })
	if count, err := db.Maintenance().AdoptOwnedStorage(ctx, rel, n.ID, []store.LocalMedia{proof}, audit); err == nil || count != 0 {
		t.Fatal("unaudited adoption committed", count, err)
	}
	var count int
	if err = raw.QueryRow("SELECT COUNT(*) FROM cluster_upload_sessions WHERE media_id=?", objectID).Scan(&count); err != nil || count != 0 {
		t.Fatal("publication escaped rollback", count, err)
	}
	if _, err = raw.Exec(drop); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		want := 1
		if i == 1 {
			want = 0
		}
		count, err := db.Maintenance().AdoptOwnedStorage(ctx, rel, n.ID, []store.LocalMedia{proof}, audit)
		if err != nil || count != want {
			t.Fatal(count, err)
		}
	}
	var used, reserved int64
	if err = raw.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", n.ID).Scan(&used, &reserved); err != nil || used != 42 || reserved != 0 {
		t.Fatal("quota changed", used, reserved, err)
	}
	bad := proof
	bad.Bytes++
	if _, err = db.Maintenance().AdoptOwnedStorage(ctx, rel, n.ID, []store.LocalMedia{bad}, audit); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("changed physical proof accepted", err)
	}
	if _, err = raw.Exec("UPDATE node_relationships SET state='revoked',status='offline' WHERE relationship_id=?", rel); err != nil {
		t.Fatal(err)
	}
	if _, err = db.Maintenance().AdoptOwnedStorage(ctx, rel, n.ID, []store.LocalMedia{proof}, audit); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("revoked authority accepted", err)
	}
}
