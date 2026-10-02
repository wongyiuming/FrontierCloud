package vault

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestOpenExistingDoesNotCreateAndRejectsUnsafeKeys(t *testing.T) {
	root := t.TempDir()
	missing := filepath.Join(root, "missing")
	if _, err := OpenExisting(missing); err == nil {
		t.Fatal("created missing vault")
	}
	if _, err := os.Stat(missing); !os.IsNotExist(err) {
		t.Fatal("missing directory created", err)
	}
	if _, err := OpenExisting(root); err == nil {
		t.Fatal("created missing key")
	}
	if _, err := os.Stat(filepath.Join(root, "node-vault.key")); !os.IsNotExist(err) {
		t.Fatal(err)
	}
	first, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	token, err := first.Seal("existing identity")
	if err != nil {
		t.Fatal(err)
	}
	second, err := OpenExisting(root)
	if err != nil {
		t.Fatal(err)
	}
	plain, err := second.Unseal(token)
	if err != nil || plain != "existing identity" {
		t.Fatal(plain, err)
	}
	if err = os.WriteFile(filepath.Join(root, "node-vault.key"), []byte(strings.Repeat("x", 129)), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = OpenExisting(root); err == nil {
		t.Fatal("oversized key admitted")
	}
}
