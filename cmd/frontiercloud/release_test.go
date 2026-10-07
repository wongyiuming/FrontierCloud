package main

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func TestPrepareReleaseRequiresExistingStoreClosedFenceAndCompatibleGeneration(t *testing.T) {
	directory := t.TempDir()
	data := filepath.Join(directory, "data")
	dbPath := filepath.Join(data, "node.sqlite")
	secrets := filepath.Join(directory, "secrets")
	t.Setenv("DB_TYPE", "sqlite")
	t.Setenv("SQLITE_PATH", dbPath)
	t.Setenv("DATA_ROOT", data)
	t.Setenv("SECRETS_DIR", secrets)
	if err := prepareReleaseCommand([]string{"--confirm-generation", "3"}); err == nil {
		t.Fatal("future generation accepted")
	}
	if err := prepareReleaseCommand([]string{"--confirm-generation", "2"}); !errors.Is(err, maintenance.ErrState) {
		t.Fatal("open fence admitted preparation", err)
	}
	if _, err := os.Stat(dbPath); !os.IsNotExist(err) {
		t.Fatal("created absent database", err)
	}
	db, err := sqlitestore.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err = db.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	identity, err := node.Initialize(context.Background(), db.Nodes(), secrets)
	if err != nil {
		t.Fatal(err)
	}
	if err = maintenanceCommand([]string{"enter"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "windows" {
		t.Skip("initializer ownership repair requires Unix; fence and non-creation checks passed")
	}
	if err = prepareReleaseCommand([]string{"--confirm-generation", "2"}); err != nil {
		t.Fatal(err)
	}
	row, err := db.Nodes().ReadIdentity(context.Background())
	if err != nil || row.ID != identity.ID || row.PrivateKey != identity.PrivateKey {
		t.Fatal("preparation changed identity", err)
	}
	gate, err := maintenance.Open(data)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	if enabled, err := gate.Enabled(); err != nil || !enabled {
		t.Fatal("preparation reopened fence", err)
	}
	t.Setenv("SQLITE_PATH", filepath.Join(directory, "absent.sqlite"))
	if err = prepareReleaseCommand([]string{"--confirm-generation", "2"}); err == nil {
		t.Fatal("missing store admitted")
	}
}
