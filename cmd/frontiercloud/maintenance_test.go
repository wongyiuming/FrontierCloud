package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func TestMaintenanceCommandsPreserveRoleKeepFailureClosedAndNeverRestore(t *testing.T) {
	directory := t.TempDir()
	databasePath := filepath.Join(directory, "node.sqlite")
	db, err := sqlitestore.Open(databasePath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Nodes().InitializeIdentity(context.Background(), store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "private-fixture"}); err != nil {
		t.Fatal(err)
	}
	root := filepath.Join(directory, "data")
	t.Setenv("DB_TYPE", "sqlite")
	t.Setenv("SQLITE_PATH", databasePath)
	t.Setenv("DATA_ROOT", root)
	var output bytes.Buffer
	if err := maintenanceCommand([]string{"enter"}, &output); err != nil {
		t.Fatal(err)
	}
	var report maintenanceReport
	if err := json.Unmarshal(output.Bytes(), &report); err != nil || !report.Enabled || !report.NativeQuiescent || report.RestoreReady || report.Database == nil || report.Database.Role != "Standalone" {
		t.Fatal(report, err)
	}
	settings, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	called := false
	if err := guardedCommand(settings, func(context.Context) error { called = true; return nil }); !errors.Is(err, maintenance.ErrEnabled) || called {
		t.Fatal("initializer bypassed gate", err)
	}
	if err := command([]string{"migrate"}); !errors.Is(err, maintenance.ErrEnabled) {
		t.Fatal("schema migration bypassed fence", err)
	}
	output.Reset()
	if err := maintenanceCommand([]string{"status"}, &output); err != nil {
		t.Fatal(err)
	}
	missing := filepath.Join(directory, "missing.sqlite")
	t.Setenv("SQLITE_PATH", missing)
	if err := serve(); !errors.Is(err, maintenance.ErrEnabled) {
		t.Fatal("native startup bypassed gate", err)
	}
	if err := maintenanceCommand([]string{"resume"}, &bytes.Buffer{}); err == nil {
		t.Fatal("missing database reopened gate")
	}
	if _, err := os.Stat(missing); !os.IsNotExist(err) {
		t.Fatal("inspection initialized missing store", err)
	}
	gate, err := maintenance.Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	if enabled, err := gate.Enabled(); err != nil || !enabled {
		t.Fatal("failed resume reopened", err)
	}
	t.Setenv("SQLITE_PATH", databasePath)
	output.Reset()
	if err := maintenanceCommand([]string{"resume"}, &output); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(output.Bytes(), &report); err != nil || report.Enabled || report.RestoreReady {
		t.Fatal(report, err)
	}
	if err := guardedCommand(settings, func(context.Context) error { called = true; return nil }); err != nil || !called {
		t.Fatal("resume did not admit normal commands", err)
	}
	identity, err := db.Nodes().ReadIdentity(context.Background())
	if err != nil || identity.Role != "Standalone" || identity.PrivateKey != "private-fixture" {
		t.Fatal("maintenance changed identity", err)
	}
}
