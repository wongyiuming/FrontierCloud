package bootstrap

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestInitializeSecretsIsPersistent(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("secret ownership contract requires root")
	}
	root := t.TempDir()
	if err := InitializeSecrets(root); err != nil {
		t.Fatal(err)
	}
	before := map[string]string{}
	for _, name := range secretNames {
		value, err := os.ReadFile(filepath.Join(root, name))
		if err != nil {
			t.Fatal(err)
		}
		before[name] = string(value)
	}
	if err := InitializeSecrets(root); err != nil {
		t.Fatal(err)
	}
	for name, expected := range before {
		value, _ := os.ReadFile(filepath.Join(root, name))
		if string(value) != expected {
			t.Fatalf("%s changed during repeated initialization", name)
		}
	}
	announcement, err := os.ReadFile(filepath.Join(root, ".announce-once"))
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	if err := json.Unmarshal(announcement, &names); err != nil || len(names) != len(secretNames) {
		t.Fatalf("invalid announcement: %q %v", announcement, err)
	}
}

func TestInitializeMediaCreatesLayout(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("media ownership contract requires root")
	}
	root := t.TempDir()
	if err := InitializeMedia(root); err != nil {
		t.Fatal(err)
	}
	for _, relative := range mediaDirectories {
		if info, err := os.Stat(filepath.Join(root, filepath.FromSlash(relative))); err != nil || !info.IsDir() {
			t.Fatalf("missing %s", relative)
		}
	}
}

func TestInitializeMediaDoesNotFollowDirectorySymlinks(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("media ownership contract requires root")
	}
	root := t.TempDir()
	outside := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "media"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(root, "media", "music")); err != nil {
		t.Skipf("symlink unavailable: %v", err)
	}
	if err := InitializeMedia(root); err == nil {
		t.Fatal("initializer followed external directory symlink")
	}
	entries, err := os.ReadDir(outside)
	if err != nil || len(entries) != 0 {
		t.Fatalf("outside volume changed: %v %v", entries, err)
	}
}

func TestMediaPermissionRepairKeepsPrivateDirectoriesPrivate(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("media ownership contract requires root")
	}
	root := t.TempDir()
	for _, name := range []string{"media/music/album", "media/.delete-private", "recordings", "secrets"} {
		if err := os.MkdirAll(filepath.Join(root, name), 0700); err != nil {
			t.Fatal(err)
		}
	}
	if err := InitializeMedia(root); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"media/music/album", "media/.delete-private", "recordings", "secrets"} {
		info, err := os.Stat(filepath.Join(root, name))
		if err != nil {
			t.Fatal(err)
		}
		public := name == "media/music/album"
		if public && info.Mode().Perm()&0o055 != 0o055 || !public && info.Mode().Perm()&0o077 != 0 {
			t.Fatalf("unexpected mode %s: %o", name, info.Mode().Perm())
		}
	}
}
