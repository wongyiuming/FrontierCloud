package sqlite

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestNativeSQLitePrivateStoreAndJournalModes(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX read-group permission contract")
	}
	name := filepath.Join(t.TempDir(), "private.db")
	db, err := Open(name)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, suffix := range []string{"", "-wal", "-shm"} {
		info, err := os.Stat(name + suffix)
		if err != nil || info.Mode().Perm() != 0600 {
			t.Fatal("private authoritative store exposed to a read group", suffix, info, err)
		}
	}
}

func TestNativeSQLiteWriterRepairsOnlySelectedFileAndReadonlyDoesNotChmod(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX read-group permission contract")
	}
	name := filepath.Join(t.TempDir(), "private.db")
	db, err := Open(name)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(name, 0644); err != nil {
		t.Fatal(err)
	}
	db, err = OpenReadOnly(context.Background(), name)
	if err != nil {
		t.Fatal(err)
	}
	db.Close()
	info, err := os.Stat(name)
	if err != nil || info.Mode().Perm() != 0644 {
		t.Fatal("read-only inspection mutated file permissions", err)
	}
	db, err = OpenExisting(context.Background(), name)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	info, err = os.Stat(name)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("native writer did not protect its selected store", err)
	}
}

func TestNativeSQLiteRejectsNonRegularSelectedWriterPath(t *testing.T) {
	dir := t.TempDir()
	for _, open := range []func(string) (*Store, error){Open, func(s string) (*Store, error) { return OpenExisting(context.Background(), s) }} {
		if db, err := open(dir); err == nil {
			db.Close()
			t.Fatal("directory admitted as SQLite")
		}
	}
}
