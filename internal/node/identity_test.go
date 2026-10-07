package node

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	"github.com/wongyiuming/FrontierCloud/internal/vault"
)

func TestIdentityRestartAndMissingSecretFailClosed(t *testing.T) {
	dir := t.TempDir()
	db, err := sqlitestore.Open(filepath.Join(dir, "node.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	if err := db.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	secrets := filepath.Join(dir, "secrets")
	first, err := Initialize(ctx, db.Nodes(), secrets)
	if err != nil {
		t.Fatal(err)
	}
	second, err := Initialize(ctx, db.Nodes(), secrets)
	if err != nil || first.ID != second.ID || first.PrivateKey != second.PrivateKey {
		t.Fatal("identity changed on restart", err)
	}
	token, err := first.KaraokeHandle("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", false)
	if err != nil {
		t.Fatal(err)
	}
	v, err := vault.Open(secrets)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := v.Unseal(token)
	if err != nil {
		t.Fatal(err)
	}
	var handle map[string]any
	if err := json.Unmarshal([]byte(raw), &handle); err != nil || handle["kind"] != "standalone" || handle["v"] != float64(1) {
		t.Fatalf("handle: %s %v", raw, err)
	}
	encoded, err := v.Unseal(first.PrivateKey)
	if err != nil {
		t.Fatal(err)
	}
	if public, err := protocol.PublicKey(encoded); err != nil || len(public) != 43 {
		t.Fatalf("persisted key incompatible: %v", err)
	}
	if err := os.Remove(filepath.Join(secrets, "node-vault.key")); err != nil {
		t.Fatal(err)
	}
	if _, err := Initialize(ctx, db.Nodes(), secrets); err == nil {
		t.Fatal("missing persistent secret silently replaced identity")
	}
}
