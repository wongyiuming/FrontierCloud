package media

import (
	"archive/zip"
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"
)

func TestDownloadStreamingArchiveDeduplicatesAndProtectsFallback(t *testing.T) {
	root, _, svc := deleteFixture(t)
	if err := os.WriteFile(filepath.Join(root, "music/artist/non-media.txt"), []byte("not managed"), 0644); err != nil {
		t.Fatal(err)
	}
	download, err := svc.Download(context.Background(), []string{"music/artist", "music/artist/song.mp3", "lyrics/artist"})
	if err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	err = download.ZIP(&output)
	download.Close()
	download.Close()
	if err != nil {
		t.Fatal(err)
	}
	archive, err := zip.NewReader(bytes.NewReader(output.Bytes()), int64(output.Len()))
	if err != nil {
		t.Fatal(err)
	}
	if len(archive.File) != 2 {
		t.Fatalf("duplicate or unmanaged archive entries: %d", len(archive.File))
	}
	for _, file := range archive.File {
		if file.Method != zip.Store {
			t.Fatal("archive recompressed media")
		}
		reader, err := file.Open()
		if err != nil {
			t.Fatal(err)
		}
		payload, err := io.ReadAll(reader)
		reader.Close()
		if err != nil || string(payload) != "ID3payload" {
			t.Fatalf("archive corruption: %s %v", payload, err)
		}
	}
	for _, name := range []string{"lyrics", defaultLyric, "../outside"} {
		if bad, err := svc.Download(context.Background(), []string{name}); err == nil {
			bad.Close()
			t.Fatalf("protected download accepted: %s", name)
		}
	}
	if err := os.Chmod(filepath.Join(root, "music/artist/song.mp3"), 0600); err != nil {
		t.Fatal(err)
	}
	single, err := svc.Download(context.Background(), []string{"music/artist/song.mp3"})
	if err != nil {
		t.Fatal(err)
	}
	f, _, err := single.Open(single.Items[0].Path)
	if err != nil {
		t.Fatal(err)
	}
	f.Close()
	single.Close()
	info, err := os.Stat(filepath.Join(root, "music/artist/song.mp3"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm()&0044 != 0044 {
		t.Fatal("Nginx readability not repaired")
	}
}

func TestArchiveHonorsCanceledContext(t *testing.T) {
	_, _, svc := deleteFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	download, err := svc.Download(ctx, []string{"music"})
	if err != nil {
		t.Fatal(err)
	}
	defer download.Close()
	cancel()
	var out bytes.Buffer
	if err := download.ZIP(&out); !errors.Is(err, context.Canceled) {
		t.Fatalf("archive cancellation ignored: %v", err)
	}
}
