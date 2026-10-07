package main

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func TestBackupCacheCleanupRequiresClosedFenceCurrentIdentityAndDurableAudit(t *testing.T) {
	root := t.TempDir()
	dbPath := filepath.Join(root, "node.sqlite")
	secrets := filepath.Join(root, "secrets")
	t.Setenv("DB_TYPE", "sqlite")
	t.Setenv("SQLITE_PATH", dbPath)
	t.Setenv("DATA_ROOT", root)
	t.Setenv("SECRETS_DIR", secrets)
	db, err := sqlitestore.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	if err := db.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	identity, err := node.Initialize(ctx, db.Nodes(), secrets)
	if err != nil {
		t.Fatal(err)
	}
	cache := filepath.Join(root, ".business-backups")
	os.Mkdir(cache, 0700)
	name := "business-" + strings.Repeat("a", 32) + ".jsonl"
	payload := filepath.Join(cache, name)
	os.WriteFile(payload, []byte("interrupted private export"), 0600)
	os.WriteFile(payload+".owner", []byte("native-backup-artifact-v1\n"), 0600)
	os.WriteFile(filepath.Join(cache, "foreign"), []byte("keep"), 0600)
	args := []string{"--confirm-node-id", identity.ID}
	var out bytes.Buffer
	if err := cleanupBackupCacheCommand(args, &out); !errors.Is(err, maintenance.ErrState) {
		t.Fatal("open fence allowed cleanup", err)
	}
	if err := maintenanceCommand([]string{"enter"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if err := cleanupBackupCacheCommand([]string{"--confirm-node-id", strings.Repeat("b", 32)}, &out); err == nil {
		t.Fatal("wrong node confirmation accepted")
	}
	if _, err := db.Database().Exec("CREATE TRIGGER cache_audit_fault BEFORE INSERT ON admin_audit_log BEGIN SELECT RAISE(ABORT,'injected'); END"); err != nil {
		t.Fatal(err)
	}
	if err := cleanupBackupCacheCommand(args, &out); err == nil {
		t.Fatal("audit failure ignored")
	}
	if got, _ := os.ReadFile(payload); string(got) != "interrupted private export" {
		t.Fatal("audit failure erased artifact")
	}
	if out.Len() != 0 {
		t.Fatal("failed cleanup emitted success")
	}
	db.Database().Exec("DROP TRIGGER cache_audit_fault")
	if err := cleanupBackupCacheCommand(args, &out); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(payload); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("owned artifact retained", err)
	}
	if got, _ := os.ReadFile(filepath.Join(cache, "foreign")); string(got) != "keep" {
		t.Fatal("unknown cache erased")
	}
	if !strings.Contains(out.String(), `"removed":1`) || !strings.Contains(out.String(), `"retained_unknown":1`) {
		t.Fatal("count-only report missing", out.String())
	}
	row, err := db.Nodes().ReadIdentity(ctx)
	if err != nil || row.ID != identity.ID || row.Role != "Standalone" || row.PrivateKey != identity.PrivateKey {
		t.Fatal("cleanup changed identity", err)
	}
	gate, err := maintenance.Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	if enabled, err := gate.Enabled(); err != nil || !enabled {
		t.Fatal("cleanup reopened fence", err)
	}
	var audits int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='backup_cache_cleanup' AND result IN ('pending','success')").Scan(&audits); err != nil || audits != 2 {
		t.Fatal("durable cleanup audit missing", audits, err)
	}
}
