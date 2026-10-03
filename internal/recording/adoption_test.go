package recording

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestSignedRecordingAdoptionCompletePhysicalProofNoBytesOrQuotaMutation(t *testing.T) {
	s, db, user, local := storageFixture(t)
	ctx := context.Background()
	raw := db.Database()
	master, relationship, id := strings.Repeat("e", 32), strings.Repeat("f", 32), strings.Repeat("c", 32)
	seed := protocol.Encode(make([]byte, 32))
	key := ed25519.NewKeyFromSeed(make([]byte, 32))
	public := protocol.Encode(key.Public().(ed25519.PublicKey))
	now := time.Now().Unix()
	if _, err := raw.Exec("UPDATE node_identity SET `role`='Follower' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	pack := store.PairPackage{Nonce: master, TokenHash: strings.Repeat("e", 64), ExpiresAt: now + 300}
	if _, err := db.Nodes().IssuePair(ctx, pack, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	rel := store.Relationship{ID: relationship, PeerID: master, PublicKey: public, Endpoint: "https://old-master.test", Credential: "sealed-fixture", Direction: "upstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: now}
	if err := db.Nodes().ConsumePair(ctx, pack, rel, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := db.Nodes().ActivateRelationship(ctx, relationship, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled = true
	cfg.Storage.Allocation = 5 * store.GiB
	if err := db.Pool().AcceptFollowerConfiguration(ctx, relationship, cfg, 5*store.GiB); err != nil {
		t.Fatal(err)
	}
	payload := []byte("historical recording bytes")
	sum := sha256.Sum256(payload)
	inventory := store.RecordingInventory{Kind: "frontiercloud-recording-inventory", Version: 1, SchemaGeneration: 2, MasterID: master, FollowerID: local, Relationship: relationship, CreatedAt: now, ExpiresAt: now + 1800, Recordings: []store.RecordingProof{{ID: id, UserID: user.ID, Filename: "录音.webm", ContentType: "audio/webm", Bytes: int64(len(payload)), SHA256: hex.EncodeToString(sum[:]), CreatedAt: now - 1}}}
	sign := func(v store.RecordingInventory) store.SignedRecordingInventory {
		t.Helper()
		canonical, err := v.CanonicalPayload()
		if err != nil {
			t.Fatal(err)
		}
		signature, err := protocol.Sign(seed, canonical)
		if err != nil {
			t.Fatal(err)
		}
		return store.SignedRecordingInventory{Payload: v, Signature: signature}
	}
	signed := sign(inventory)
	name, _ := Path(relationship, user.ID, id)
	directory := s.root.Name()
	full := filepath.Join(directory, filepath.FromSlash(name))
	if err := os.MkdirAll(filepath.Dir(full), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(full, payload, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("UPDATE cluster_storage_members SET used_bytes=? WHERE member_id=?", len(payload), local); err != nil {
		t.Fatal(err)
	}
	adopt := func(v store.SignedRecordingInventory) (int, error) {
		return AdoptOwnedRecordings(ctx, directory, db.Maintenance(), db.Recordings(), db.Nodes(), v, local)
	}
	cases := []struct {
		name     string
		prepare  func()
		undo     func()
		manifest store.SignedRecordingInventory
	}{
		{name: "unregistered", prepare: func() { os.WriteFile(filepath.Join(directory, "unknown"), []byte("keep"), 0600) }, undo: func() { os.Remove(filepath.Join(directory, "unknown")) }, manifest: signed},
		{name: "legacy-part", prepare: func() { os.WriteFile(strings.TrimSuffix(full, ".bin")+".part", payload, 0600) }, undo: func() { os.Remove(strings.TrimSuffix(full, ".bin") + ".part") }, manifest: signed},
		{name: "native-intent", prepare: func() { os.WriteFile(filepath.Join(directory, ".recording-"+id+".json"), []byte("keep"), 0600) }, undo: func() { os.Remove(filepath.Join(directory, ".recording-"+id+".json")) }, manifest: signed},
		{name: "changed-digest", prepare: func() { os.WriteFile(full, bytes.Repeat([]byte("x"), len(payload)), 0600) }, undo: func() { os.WriteFile(full, payload, 0600) }, manifest: signed},
		{name: "missing", prepare: func() { os.Rename(full, full+".missing") }, undo: func() { os.Rename(full+".missing", full) }, manifest: signed},
	}
	omitted := inventory
	omitted.Recordings = []store.RecordingProof{}
	cases = append(cases, struct {
		name     string
		prepare  func()
		undo     func()
		manifest store.SignedRecordingInventory
	}{name: "omitted", manifest: sign(omitted)})
	expired := inventory
	expired.CreatedAt = now - 1900
	expired.ExpiresAt = expired.CreatedAt + 1800
	cases = append(cases, struct {
		name     string
		prepare  func()
		undo     func()
		manifest store.SignedRecordingInventory
	}{name: "expired", manifest: sign(expired)})
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			if test.prepare != nil {
				test.prepare()
				defer test.undo()
			}
			if n, err := adopt(test.manifest); err == nil || n != 0 {
				t.Fatal("unsafe physical adoption", n, err)
			}
			if row, err := db.Recordings().Recording(ctx, id); err != nil || row != nil {
				t.Fatal("partial row", row, err)
			}
		})
	}
	unlockVolume, err := s.acquire(ctx, true)
	if err != nil {
		t.Fatal(err)
	}
	bounded, cancel := context.WithTimeout(ctx, 25*time.Millisecond)
	_, err = AdoptOwnedRecordings(bounded, directory, db.Maintenance(), db.Recordings(), db.Nodes(), signed, local)
	cancel()
	unlockVolume()
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("live volume lease bypassed", err)
	}
	link := filepath.Join(directory, "linked")
	if err := os.Symlink(filepath.Dir(full), link); err == nil {
		if _, err := adopt(signed); !errors.Is(err, ErrRecovery) {
			t.Fatal("linked bytes accepted", err)
		}
		os.Remove(link)
	} else {
		t.Log("symlink privilege unavailable; exercised on Linux")
	}
	// The pinned key may change after the service's pre-scan verification.
	changed := &adoptionAuthorityRace{MaintenanceRepository: db.Maintenance(), before: func() {
		if _, err := raw.Exec("UPDATE node_relationships SET peer_key=? WHERE relationship_id=?", strings.Repeat("A", 43), relationship); err != nil {
			t.Fatal(err)
		}
	}}
	if _, err := AdoptOwnedRecordings(ctx, directory, changed, db.Recordings(), db.Nodes(), signed, local); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("authority changed during scan accepted", err)
	}
	if _, err := raw.Exec("UPDATE node_relationships SET peer_key=? WHERE relationship_id=?", public, relationship); err != nil {
		t.Fatal(err)
	}
	raw.Exec("CREATE TRIGGER recording_adopt_failure BEFORE INSERT ON node_audit WHEN NEW.action='recording-owned-adopted' BEGIN SELECT RAISE(ABORT,'fixture'); END")
	if count, err := adopt(signed); err == nil || count != 0 {
		t.Fatal("audit failure ignored", count, err)
	}
	raw.Exec("DROP TRIGGER recording_adopt_failure")
	for i := 0; i < 2; i++ {
		if count, err := adopt(signed); err != nil || count != 1-i {
			t.Fatal(count, err)
		}
	}
	if got, err := os.ReadFile(full); err != nil || !bytes.Equal(got, payload) {
		t.Fatal("bytes changed", err)
	}
	var used, reserved int64
	raw.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", local).Scan(&used, &reserved)
	if used != int64(len(payload)) || reserved != 0 {
		t.Fatal("double accounting", used, reserved)
	}
	// Adopted historical bytes now use the normal durable delete/refund path.
	unlock, err := s.Lock(id)
	if err != nil {
		t.Fatal(err)
	}
	err = s.Delete(ctx, relationship, user.ID, id, store.KaraokeAudit{}, store.NodeAudit{})
	unlock()
	if err != nil {
		t.Fatal(err)
	}
	raw.QueryRow("SELECT used_bytes FROM cluster_storage_members WHERE member_id=?", local).Scan(&used)
	if used != 0 {
		t.Fatal("refund", used)
	}
	if _, err := adopt(signed); !errors.Is(err, ErrRecovery) {
		t.Fatal("deleted recording resurrected", err)
	}
}

type adoptionAuthorityRace struct {
	store.MaintenanceRepository
	before func()
}

func (r *adoptionAuthorityRace) AdoptOwnedRecordings(ctx context.Context, v store.SignedRecordingInventory, id string, a store.NodeAudit) (int, error) {
	r.before()
	return r.MaintenanceRepository.AdoptOwnedRecordings(ctx, v, id, a)
}

func TestRecordingInventoryParserRejectsAmbiguityAndOversize(t *testing.T) {
	value := store.SignedRecordingInventory{Payload: store.RecordingInventory{Kind: "frontiercloud-recording-inventory", Version: 1, SchemaGeneration: 2, Recordings: []store.RecordingProof{}}, Signature: "fixture"}
	raw, _ := json.Marshal(value)
	if got, err := ParseInventory(bytes.NewReader(raw)); err != nil || got.Signature != value.Signature {
		t.Fatal(got, err)
	}
	for _, bad := range []string{string(raw) + " {}", strings.Replace(string(raw), `"signature":"fixture"`, `"signature":"fixture","signature":"other"`, 1), strings.Replace(string(raw), `"version":1`, `"version":1,"unknown":0`, 1), strings.Replace(string(raw), `"recordings":[]`, `"recordings":null`, 1), strings.Repeat(" ", store.MaxRecordingInventoryBytes+1)} {
		if _, err := ParseInventory(strings.NewReader(bad)); err == nil {
			t.Fatal("ambiguous inventory accepted")
		}
	}
}
