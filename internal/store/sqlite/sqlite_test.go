package sqlite

import (
	"context"
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
