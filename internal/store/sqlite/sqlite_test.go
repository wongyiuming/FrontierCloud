package sqlite

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestOpenAppliesSQLitePolicy(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "frontiercloud.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	checks := map[string]string{
		"foreign_keys": "1",
		"journal_mode": "wal",
		"busy_timeout": "5000",
		"synchronous":  "1",
	}
	for pragma, expected := range checks {
		var actual string
		if err := store.Database().QueryRowContext(context.Background(), "PRAGMA "+pragma).Scan(&actual); err != nil {
			t.Fatalf("read %s: %v", pragma, err)
		}
		if actual != expected {
			t.Fatalf("%s=%s, want %s", pragma, actual, expected)
		}
	}
}

func TestReadOnlyOpenNeverCreatesOrMutatesAuthoritativeDatabase(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "missing", "node.sqlite")
	if store, err := OpenReadOnly(ctx, path); err == nil {
		store.Close()
		t.Fatal("read-only open created an absent store")
	}
	if _, err := os.Stat(filepath.Dir(path)); !os.IsNotExist(err) {
		t.Fatal("read-only open created a parent directory", err)
	}
	writer, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := writer.Database().Exec("CREATE TABLE fixture(value INTEGER); INSERT INTO fixture VALUES (42)"); err != nil {
		t.Fatal(err)
	}
	writer.Close()
	reader, err := OpenReadOnly(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	var value int
	if err := reader.Database().QueryRow("SELECT value FROM fixture").Scan(&value); err != nil || value != 42 {
		t.Fatal(value, err)
	}
	if _, err := reader.Database().Exec("UPDATE fixture SET value=0"); err == nil {
		t.Fatal("inspection handle admitted SQL writes")
	}
	cancelled, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := OpenReadOnly(cancelled, path); !errors.Is(err, context.Canceled) {
		t.Fatal("read-only initialization ignored cancellation", err)
	}
}
