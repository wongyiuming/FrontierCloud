package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func TestRecordingMaintenanceCommandsExportPinnedMasterAndAdoptWithoutInitializingAccounts(t *testing.T) {
	ctx := context.Background()
	now := time.Now().Unix()
	relationship := strings.Repeat("d", 32)
	user := strings.Repeat("e", 32)
	id := strings.Repeat("f", 32)
	type fixture struct {
		root, secrets, path string
		db                  *sqlite.Store
		identity            *node.Identity
		public              string
		gate                *maintenance.Gate
	}
	create := func(role string) fixture {
		t.Helper()
		root := t.TempDir()
		name := filepath.Join(root, "node.db")
		secrets := filepath.Join(root, "secrets")
		db, err := sqlite.Open(name)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { db.Close() })
		if err = db.Initialize(ctx); err != nil {
			t.Fatal(err)
		}
		identity, err := node.Initialize(ctx, db.Nodes(), secrets)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = db.Nodes().PromoteIdentity(ctx, store.NodePromotion{Role: role, Endpoint: "https://" + strings.ToLower(role) + ".test", Allocation: 2 * store.GiB, PhysicalFree: 5 * store.GiB}, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		envelope, err := node.NewService(db.Nodes(), identity, nil).SignedIdentity(ctx, user)
		if err != nil {
			t.Fatal(err)
		}
		gate, err := maintenance.Open(root)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { gate.Close() })
		return fixture{root, secrets, name, db, identity, envelope.Payload["public_key"].(string), gate}
	}
	master, follower := create("Master"), create("Follower")
	rel := store.Relationship{ID: relationship, PeerID: follower.identity.ID, PublicKey: follower.public, Endpoint: "https://follower.test", Credential: "sealed-fixture", Direction: "downstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: now}
	if err := master.db.Nodes().PrepareRelationship(ctx, rel, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := master.db.Nodes().ActivateRelationship(ctx, relationship, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled = true
	cfg.Storage.Allocation = 2 * store.GiB
	if err := master.db.Pool().ConfigureMember(ctx, follower.identity.ID, cfg, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	pack := store.PairPackage{Nonce: user, TokenHash: strings.Repeat("c", 64), ExpiresAt: now + 300}
	if _, err := follower.db.Nodes().IssuePair(ctx, pack, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	rel.PeerID = master.identity.ID
	rel.PublicKey = master.public
	rel.Endpoint = "https://master.test"
	rel.Direction = "upstream"
	if err := follower.db.Nodes().ConsumePair(ctx, pack, rel, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := follower.db.Nodes().ActivateRelationship(ctx, relationship, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := follower.db.Pool().AcceptFollowerConfiguration(ctx, relationship, cfg, 5*store.GiB); err != nil {
		t.Fatal(err)
	}
	data := []byte("legacy recorded media")
	sum := sha256.Sum256(data)
	if _, err := master.db.Database().Exec("INSERT INTO karaoke_recordings(recording_id,user_id,storage_member_id,filename,content_type,size_bytes,sha256,state,title,lyrics,created_at,updated_at) VALUES (?,?,?,'旧录音.webm','audio/webm',?,?,'ready','','[]',?,?)", id, user, follower.identity.ID, len(data), hex.EncodeToString(sum[:]), now-1, now); err != nil {
		t.Fatal(err)
	}
	for _, f := range []fixture{master, follower} {
		if _, err := f.db.Database().Exec("UPDATE cluster_storage_members SET used_bytes=? WHERE member_id=?", len(data), follower.identity.ID); err != nil {
			t.Fatal(err)
		}
	}
	selectFixture := func(f fixture) {
		t.Setenv("DB_TYPE", "sqlite")
		t.Setenv("SQLITE_PATH", f.path)
		t.Setenv("DATA_ROOT", f.root)
		t.Setenv("SECRETS_DIR", f.secrets)
	}
	selectFixture(master)
	exportArgs := []string{"--relationship", relationship, "--confirm-node-id", master.identity.ID}
	var exported bytes.Buffer
	if err := recordingAdoptionCommand(exportArgs, &exported, true); !errors.Is(err, maintenance.ErrState) {
		t.Fatal("export without closed fence", err)
	}
	if err := master.gate.Enter(ctx, func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if err := recordingAdoptionCommand(exportArgs, &exported, true); err != nil {
		t.Fatal("readonly Master export", err)
	}
	inventory := filepath.Join(t.TempDir(), "signed-inventory.json")
	if err := os.WriteFile(inventory, exported.Bytes(), 0600); err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(follower.root, "recordings", relationship, user, id+".bin")
	if err := os.MkdirAll(filepath.Dir(file), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(file, data, 0600); err != nil {
		t.Fatal(err)
	}
	selectFixture(follower)
	args := []string{"--relationship", relationship, "--confirm-node-id", follower.identity.ID, "--file", inventory}
	var out bytes.Buffer
	if err := recordingAdoptionCommand(args, &out, false); !errors.Is(err, maintenance.ErrState) {
		t.Fatal("adoption without closed fence", err)
	}
	if err := follower.gate.Enter(ctx, func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if err := recordingAdoptionCommand(args, &out, false); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), `"adopted_recordings":1`) || !strings.Contains(out.String(), `"restore_ready":false`) {
		t.Fatal(out.String())
	}
	out.Reset()
	if err := recordingAdoptionCommand(args, &out, false); err != nil || !strings.Contains(out.String(), `"adopted_recordings":0`) {
		t.Fatal("replay", out.String(), err)
	}
	var accounts int
	if err := follower.db.Database().QueryRow("SELECT COUNT(*) FROM karaoke_users").Scan(&accounts); err != nil || accounts != 0 {
		t.Fatal("fabricated accounts", accounts, err)
	}
	if enabled, err := follower.gate.Enabled(); err != nil || !enabled {
		t.Fatal("fence reopened", err)
	}
	current, err := node.OpenExisting(ctx, follower.db.Nodes(), follower.secrets)
	if err != nil || current.ID != follower.identity.ID || current.PrivateKey != follower.identity.PrivateKey {
		t.Fatal("identity changed", err)
	}
}
