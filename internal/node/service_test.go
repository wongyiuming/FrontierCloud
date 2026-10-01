package node

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	storeSQLite "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

// The in-process transport uses exactly the canonical control wire body and
// request HMAC. TLS trust and pinning are exercised separately on real sockets.
type localControl struct {
	nodes map[string]*Service
	calls int
}

func (l *localControl) Identity(ctx context.Context, origin, id, key, role string) (Peer, error) {
	l.calls++
	s := l.nodes[origin]
	if s == nil {
		return Peer{}, errors.New("unreachable node")
	}
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return Peer{}, err
	}
	public, err := s.public(row)
	if err != nil {
		return Peer{}, err
	}
	if (id != "" && row.ID != id) || (key != "" && key != public) || (role != "" && role != row.Role) {
		return Peer{}, errors.New("identity mismatch")
	}
	return Peer{row.ID, row.Endpoint, public, row.Role, AppVersion}, nil
}
func (l *localControl) Request(ctx context.Context, origin, route, method string, value any, relationship, credential string) (map[string]any, error) {
	s := l.nodes[origin]
	if s == nil {
		return nil, errors.New("unreachable node")
	}
	l.calls++
	body, err := protocol.Canonical(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var v map[string]any
	if err = decoder.Decode(&v); err != nil {
		return nil, err
	}
	if route == "/internal/v1/pair" {
		packageRaw, ok := v["package"].(map[string]any)
		if !ok {
			return nil, errors.New("pair not a wire envelope")
		}
		masterRaw, ok := v["master"].(map[string]any)
		if !ok {
			return nil, errors.New("Master not a wire envelope")
		}
		signed, err := envelope(packageRaw)
		if err != nil {
			return nil, err
		}
		master, err := envelope(masterRaw)
		if err != nil {
			return nil, err
		}
		if err = s.ConsumePair(ctx, signed, master, textField(v, "relationship_id"), textField(v, "credential")); err != nil {
			return nil, err
		}
		return map[string]any{"state": "pending", "protocol": 2}, nil
	}
	headers, err := protocol.AuthHeaders(credential, relationship, method, route, body, time.Now().Unix())
	if err != nil {
		return nil, err
	}
	h := http.Header{}
	for k, v := range headers {
		h.Set(k, v)
	}
	relation, err := s.Authenticate(ctx, h, method, route, body, route == "/internal/v1/confirm" || route == "/internal/v1/revoke", route == "/internal/v1/revoke")
	if err != nil {
		return nil, err
	}
	switch route {
	case "/internal/v1/confirm":
		err = s.repo.ActivateRelationship(ctx, relation.ID, store.NodeAudit{Actor: relation.PeerID})
	case "/internal/v1/revoke":
		err = s.repo.RevokeRelationship(ctx, relation.ID, true, store.NodeAudit{Actor: relation.PeerID})
	case "/internal/v1/heartbeat":
		return s.ReceiveHeartbeat(ctx, relation, v)
	default:
		err = errors.New("unsupported control path")
	}
	return map[string]any{"protocol": 2}, err
}
func testNode(t *testing.T, role, origin string, transport ControlClient) (*Service, *storeSQLite.Store) {
	t.Helper()
	directory := t.TempDir()
	db, err := storeSQLite.Open(filepath.Join(directory, "node.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	ctx := context.Background()
	if err = db.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	id, err := Initialize(ctx, db.Nodes(), filepath.Join(directory, "secrets"))
	if err != nil {
		t.Fatal(err)
	}
	// Test fixtures model already-promoted nodes. Production role promotion must
	// also validate physical emptiness/local allocation and is gated separately.
	if _, err = db.Database().Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", role, origin); err != nil {
		t.Fatal(err)
	}
	return NewService(db.Nodes(), id, transport), db
}
func TestPairServiceWireRoundtripEncryptedSecretsReplayAndRePair(t *testing.T) {
	ctx := context.Background()
	transport := &localControl{nodes: map[string]*Service{}}
	master, _ := testNode(t, "Master", "https://master.test", transport)
	follower, followerDB := testNode(t, "Follower", "https://follower.test", transport)
	transport.nodes["https://master.test"] = master
	transport.nodes["https://follower.test"] = follower
	signed, err := follower.CreatePair(ctx, store.NodeAudit{Actor: "admin"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(follower.identity.PrivateKey, textField(signed.Payload, "token")) {
		t.Fatal("token leaked into persistent identity")
	}
	identifier, err := master.ImportPair(ctx, signed, store.NodeAudit{Actor: "admin"})
	if err != nil {
		t.Fatal(err)
	}
	mr, err := master.repo.Relationship(ctx, identifier)
	if err != nil || mr.State != "active" || mr.Direction != "downstream" {
		t.Fatalf("Master pair %+v %v", mr, err)
	}
	fr, err := follower.repo.Relationship(ctx, identifier)
	if err != nil || fr.State != "active" || fr.Direction != "upstream" {
		t.Fatalf("Follower pair %+v %v", fr, err)
	}
	credential, err := master.identity.vault.Unseal(mr.Credential)
	if err != nil {
		t.Fatal(err)
	}
	followerCredential, err := follower.identity.vault.Unseal(fr.Credential)
	if err != nil || credential != followerCredential || credential == mr.Credential || credential == fr.Credential {
		t.Fatal("credential vault interoperability", err)
	}
	status, err := master.Status(ctx)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(status)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(encoded, []byte(credential)) || bytes.Contains(encoded, []byte(mr.Credential)) || bytes.Contains(encoded, []byte(mr.PublicKey)) {
		t.Fatal("relationship secrets exposed in status")
	}
	body := []byte(`{}`)
	headers, err := protocol.AuthHeaders(credential, identifier, "POST", "/internal/v1/heartbeat?scope=test", body, time.Now().Unix())
	if err != nil {
		t.Fatal(err)
	}
	h := http.Header{}
	for k, v := range headers {
		h.Set(k, v)
	}
	if _, err = follower.Authenticate(ctx, h, "POST", "/internal/v1/heartbeat?scope=other", body, false, false); err == nil {
		t.Fatal("query tampering accepted")
	}
	if _, err = follower.Authenticate(ctx, h, "POST", "/internal/v1/heartbeat?scope=test", body, false, false); err != nil {
		t.Fatal(err)
	}
	if _, err = follower.Authenticate(ctx, h, "POST", "/internal/v1/heartbeat?scope=test", body, false, false); err == nil {
		t.Fatal("control request replay accepted")
	}
	before := transport.calls
	challenge, _ := randomNodeID()
	masterSigned, err := master.SignedIdentity(ctx, challenge)
	if err != nil {
		t.Fatal(err)
	}
	if err = follower.ConsumePair(ctx, signed, masterSigned, strings.Repeat("e", 32), credential); err == nil {
		t.Fatal("consumed pairing package accepted")
	}
	if transport.calls != before {
		t.Fatal("unavailable pair performed outbound request")
	}
	if _, err = follower.CreatePair(ctx, store.NodeAudit{Actor: "admin"}); err == nil {
		t.Fatal("paired Follower issued another package")
	}
	if err = master.repo.RevokeRelationship(ctx, identifier, false, store.NodeAudit{Actor: "admin"}); err != nil {
		t.Fatal(err)
	}
	if err = master.NotifyRevocation(ctx, mr); err != nil {
		t.Fatal(err)
	}
	fr, _ = follower.repo.Relationship(ctx, identifier)
	mr, _ = master.repo.Relationship(ctx, identifier)
	if fr.State != "revoked" || mr.Summary["revocation_acknowledged"] != true {
		t.Fatal("revocation failed to converge")
	}
	if _, err = follower.Authenticate(ctx, h, "POST", "/internal/v1/heartbeat?scope=test", body, false, false); err == nil {
		t.Fatal("revoked credential accepted")
	}
	newPackage, err := follower.CreatePair(ctx, store.NodeAudit{Actor: "admin"})
	if err != nil {
		t.Fatal(err)
	}
	newID, err := master.ImportPair(ctx, newPackage, store.NodeAudit{Actor: "admin"})
	if err != nil || newID == identifier {
		t.Fatal("re-pair", newID, err)
	}
	var hash string
	if err = followerDB.Database().QueryRow("SELECT token_hash FROM node_pair_packages WHERE nonce=?", textField(newPackage.Payload, "nonce")).Scan(&hash); err != nil || hash != tokenDigest(textField(newPackage.Payload, "token")) {
		t.Fatal("pair token not durably hashed", err)
	}
	if _, err = master.repo.Relationship(ctx, identifier); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("old tombstone not replaced", err)
	}
}
