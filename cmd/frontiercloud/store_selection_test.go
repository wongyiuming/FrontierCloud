package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/deployment"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func TestRuntimeSelectionCannotCreateEmptyDatabaseBesideLegacyIdentity(t *testing.T) {
	r := t.TempDir()
	c := config.Config{DataRoot: r, DatabaseType: "sqlite", SQLitePath: filepath.Join(r, "original.db"), SecretsDirectory: filepath.Join(r, "secrets")}
	ctx := context.Background()
	db, err := sqlitestore.Open(c.SQLitePath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	identity, err := node.Initialize(ctx, db.Nodes(), c.SecretsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	wrong := c
	wrong.SQLitePath = filepath.Join(r, "empty.db")
	if _, err := openRuntimeStore(ctx, wrong); !errors.Is(err, deployment.ErrSelection) {
		t.Fatal("legacy identity reused in empty store", err)
	}
	if _, err := os.Stat(wrong.SQLitePath); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("refused startup created a database", err)
	}
	if _, err := os.Stat(filepath.Join(r, deployment.Binding)); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("wrong store was pinned", err)
	}
	existing, err := openRuntimeStore(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	existing.Close()
	current, err := node.OpenExisting(ctx, db.Nodes(), c.SecretsDirectory)
	if err != nil || current.ID != identity.ID {
		t.Fatal("legacy adoption replaced identity", err)
	}
}
