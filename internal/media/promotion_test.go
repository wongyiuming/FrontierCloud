package media

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestPromotionManifestCountsUnmanagedFilesAndRejectsFollowerBusiness(t *testing.T) {
	root, _, svc := deleteFixture(t)
	ctx := context.Background()
	for _, name := range []string{"music/artist/song.mp3", "music/other/song.mp3", "vido/director/video.mp4", "lyrics/artist/song.lrc"} {
		if err := os.Remove(filepath.Join(root, name)); err != nil {
			t.Fatal(err)
		}
	}
	called := false
	if err := svc.WithPromotion(ctx, "Follower", func(store.NodePromotion) error { called = true; return nil }); err != nil || !called {
		t.Fatal("empty Follower", err)
	}
	for name, value := range map[string]string{"music/album/track.mp3": "ID3track", "music/album/unmanaged.bin": "private", "music/album/.hidden.mp3": "ID3hidden", "vido/album/video.mp4": "video"} {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(root, name)), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, name), []byte(value), 0644); err != nil {
			t.Fatal(err)
		}
	}
	called = false
	if err := svc.WithPromotion(ctx, "Follower", func(store.NodePromotion) error { called = true; return nil }); !errors.Is(err, store.ErrNodeState) || called {
		t.Fatal("occupied Follower accepted", err)
	}
	if err := svc.WithPromotion(ctx, "Master", func(p store.NodePromotion) error {
		if p.PhysicalUsed != int64(len("ID3trackprivateID3hiddenvideo")) || p.PhysicalFree < 0 || len(p.Media) != 2 {
			t.Fatalf("manifest %+v", p)
		}
		for _, v := range p.Media {
			if v.Bytes <= 0 || v.ETag == "" {
				t.Fatal("incomplete media facts")
			}
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}
