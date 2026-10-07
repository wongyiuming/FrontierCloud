package brand

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestBrandPersistenceVersionAndFallback(t *testing.T) {
	data, static := t.TempDir(), t.TempDir()
	if err := os.Mkdir(filepath.Join(static, "brand"), 0755); err != nil {
		t.Fatal(err)
	}
	for _, meta := range brands {
		if err := os.WriteFile(filepath.Join(static, "brand", meta.filename), []byte("RIFFxxxxWEBPdefault"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	s, err := New(data, static)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	before, err := s.Effective("music")
	if err != nil || before.Source != "default" || before.Custom || len(before.Version) != 16 {
		t.Fatalf("default: %+v %v", before, err)
	}
	png := []byte{137, 80, 78, 71, 13, 10, 26, 10, 1, 2, 3}
	uploaded, err := s.Upload("music", png)
	if err != nil || !uploaded.Custom || uploaded.Format != "png" || uploaded.Version == before.Version {
		t.Fatalf("upload: %+v %v", uploaded, err)
	}
	reopened, err := New(data, static)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	after, err := reopened.Effective("music")
	if err != nil || after.Version != uploaded.Version {
		t.Fatal("custom logo did not survive restart")
	}
	webp, err := s.Upload("music", []byte("RIFFxxxxWEBPcustom"))
	if err != nil || webp.Format != "webp" {
		t.Fatalf("format replacement: %v", err)
	}
	if _, err := os.Stat(filepath.Join(data, "brand", "music.png")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("old format retained")
	}
	restored, changed, err := s.Delete("music")
	if err != nil || !changed || restored.Version != before.Version {
		t.Fatalf("restore: %+v %v", restored, err)
	}
	if _, changed, err := s.Delete("music"); err != nil || changed {
		t.Fatal("repeated delete not idempotent")
	}
	for _, bad := range [][]byte{nil, []byte("not an image"), make([]byte, MaxBytes+1)} {
		if _, err := s.Upload("music", bad); err == nil {
			t.Fatal("invalid upload accepted")
		}
	}
	if _, err := s.Effective("../music"); !errors.Is(err, ErrKind) {
		t.Fatal("unknown kind accepted")
	}
}
