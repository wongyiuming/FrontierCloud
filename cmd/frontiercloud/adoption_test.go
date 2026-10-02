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
)

func TestAdoptionCommandRequiresClosedFenceAndNeverCreatesStore(t *testing.T) {
	directory := t.TempDir()
	root := filepath.Join(directory, "data")
	name := filepath.Join(directory, "missing", "node.sqlite")
	t.Setenv("DATA_ROOT", root)
	t.Setenv("DB_TYPE", "sqlite")
	t.Setenv("SQLITE_PATH", name)
	args := []string{"--relationship", strings.Repeat("b", 32), "--confirm-node-id", strings.Repeat("a", 32), "--wait-seconds", "1"}
	var output bytes.Buffer
	if err := adoptStorageCommand(args, &output); !errors.Is(err, maintenance.ErrState) {
		t.Fatal("open fence allowed adoption", err)
	}
	gate, err := maintenance.Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	if err = gate.Enter(context.Background(), func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if err = adoptStorageCommand(args, &output); err == nil {
		t.Fatal("missing database adopted")
	}
	if _, err = os.Stat(filepath.Dir(name)); !os.IsNotExist(err) {
		t.Fatal("adoption initialized database", err)
	}
	if enabled, err := gate.Enabled(); err != nil || !enabled {
		t.Fatal("failed adoption resumed runtime", err)
	}
	if output.Len() != 0 {
		t.Fatal("failed adoption emitted success", output.String())
	}
	if err = adoptStorageCommand([]string{"--relationship", "not-an-id"}, &output); err == nil {
		t.Fatal("invalid arguments accepted")
	}
}
