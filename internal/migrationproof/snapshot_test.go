package migrationproof

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestFilesPreserveOrphansAndOnlyExcludeExactControlFiles(t *testing.T) {
	dir := t.TempDir()
	for name, content := range map[string]string{"orphan.lrc": "old lyric", ".native-store": "binding", ".native-runtime": "receipt", ".native-store-not-control": "retain", "admin_key": "unchanged key"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
	}
	data, err := Files(context.Background(), dir, true)
	if err != nil || len(data) != 4 {
		t.Fatal(data, err)
	}
	secrets, err := Files(context.Background(), dir, false)
	if err != nil || len(secrets) != 6 {
		t.Fatal(secrets, err)
	}
	if err := os.WriteFile(filepath.Join(dir, "orphan.lrc"), []byte("modified"), 0600); err != nil {
		t.Fatal(err)
	}
	after, err := Files(context.Background(), dir, true)
	if err != nil {
		t.Fatal(err)
	}
	if Compare(Snapshot{Data: data}, Snapshot{Data: after}) == nil {
		t.Fatal("content mismatch accepted")
	}
}

func TestFilesRejectJournalsSymlinksAndCancellation(t *testing.T) {
	for _, name := range []string{".upload-intent.json", ".delete-intent", ".rename-intent", ".recovery-required"} {
		dir := t.TempDir()
		os.WriteFile(filepath.Join(dir, name), []byte("pending"), 0600)
		if _, err := Files(context.Background(), dir, true); err == nil {
			t.Fatal("journal accepted", name)
		}
	}
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "value"), []byte("v"), 0600)
	if err := os.Symlink("value", filepath.Join(dir, ".native-store")); err == nil {
		if _, err = Files(context.Background(), dir, true); err == nil {
			t.Fatal("control symlink ignored")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := Files(ctx, dir, true); !errors.Is(err, context.Canceled) {
		t.Fatal("cancel not observed", err)
	}
}

func TestDecodeRequiresPinnedCompleteStrictProof(t *testing.T) {
	s := Snapshot{Format: Format, NodeID: "11111111111111111111111111111111", Tables: []Table{{Name: "identity"}}, Data: []File{{Path: "media.mp3"}}, Secrets: []File{{Path: "admin_key"}}}
	raw, _ := json.Marshal(s)
	parsed, err := Decode(raw, digest(raw))
	if err != nil || Compare(s, parsed) != nil {
		t.Fatal(err)
	}
	for _, bad := range [][]byte{append(append([]byte{}, raw...), []byte("{}")...), []byte(`{"format":"unknown"}`), []byte(`{"unknown":true}`)} {
		if _, err := Decode(bad, digest(bad)); err == nil {
			t.Fatal("bad proof accepted")
		}
	}
	if _, err := Decode(raw, digest([]byte("other"))); err == nil {
		t.Fatal("unverified proof accepted")
	}
	parsed.Secrets[0].Digest = "changed"
	if Compare(s, parsed) == nil {
		t.Fatal("secret mismatch accepted")
	}
}
