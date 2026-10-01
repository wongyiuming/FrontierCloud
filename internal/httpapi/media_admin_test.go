package httpapi

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/search"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestMediaAdminBusinessTreeVisibilityPriorities(t *testing.T) {
	_, db, dir, public := publicFixture(t, true)
	svc := public.media
	ctx := context.Background()
	audit := store.AdminAudit{Action: "media_priority"}
	tree, err := svc.Tree(ctx, "")
	if err != nil || len(tree.Items) != 3 {
		t.Fatalf("root tree: %+v %v", tree, err)
	}
	for _, item := range tree.Items {
		if item.Kind != "directory" || item.Size != nil {
			t.Fatalf("root item: %+v", item)
		}
	}
	if err := svc.Hide(ctx, []string{"music/nested/album", "music/nested"}, true, store.AdminAudit{Action: "hide"}); err != nil {
		t.Fatal(err)
	}
	tree, err = svc.Tree(ctx, "music/nested")
	if err != nil || len(tree.Items) != 1 || !tree.Items[0].Hidden || !tree.Items[0].HiddenDirect {
		t.Fatalf("hidden direct child: %+v %v", tree, err)
	}
	tree, err = svc.Tree(ctx, "music/nested/album")
	if err != nil || len(tree.Items) != 1 || !tree.Items[0].Hidden || tree.Items[0].HiddenDirect {
		t.Fatalf("inherited file hide: %+v %v", tree, err)
	}
	if err := svc.Hide(ctx, []string{"music/nested", "lyrics"}, false, store.AdminAudit{Action: "unhide"}); err == nil {
		t.Fatal("invalid batch accepted")
	}
	hidden, err := db.Media().HiddenPaths(ctx)
	if err != nil || !hidden["music/nested"] {
		t.Fatal("partial invalid batch modified visibility")
	}
	if err := svc.Hide(ctx, []string{"music/nested"}, false, store.AdminAudit{Action: "unhide"}); err != nil {
		t.Fatal(err)
	}
	hidden, err = db.Media().HiddenPaths(ctx)
	if err != nil || hidden["music/nested"] || hidden["music/nested/album"] {
		t.Fatalf("descendant hide not cleared: %v %v", hidden, err)
	}
	if _, err := svc.Preference(ctx, "music", 5, true, audit); err == nil {
		t.Fatal("type root preference accepted")
	}
	value, err := svc.Preference(ctx, "music/nested", 500, true, store.AdminAudit{Action: "directory_priority"})
	if err != nil || value.Path != "music/nested" || value.Preference != 500 {
		t.Fatalf("directory preference: %+v %v", value, err)
	}
	items, err := svc.DirectoryPreferences(ctx, "music")
	if err != nil || len(items) != 1 || items[0].Path != "music/nested" {
		t.Fatalf("immediate preferences: %v %v", items, err)
	}
	value, err = svc.Preference(ctx, "music/nested/album/音乐.mp3", 7, false, audit)
	if err != nil {
		t.Fatal(err)
	}
	priorities, err := svc.Priorities(ctx, "music", "", "audio", 1, 100)
	if err != nil || priorities.CatalogTotal != 2 || len(priorities.Items) != 0 || len(priorities.Directories) != 2 {
		t.Fatalf("directory priority view: %+v %v", priorities, err)
	}
	priorities, err = svc.Priorities(ctx, "music", "mp3", "audio", 8, 1)
	if err != nil || priorities.Pagination.Page != 2 || priorities.Pagination.Total != 2 || len(priorities.Items) != 1 || priorities.Items[0].MediaPath != "music/artist/song.mp3" {
		t.Fatalf("priority pagination: %+v %v", priorities, err)
	}
	priorities, err = svc.Priorities(ctx, "music/nested/album", "", "audio", 1, 100)
	if err != nil || len(priorities.Items) != 1 || priorities.Items[0].MediaID != value.MediaID || priorities.Items[0].Preference != 7 {
		t.Fatalf("priority persistence: %+v %v", priorities, err)
	}
	for _, path := range []string{"../", "/music", "music/.hidden", "music/nested/album/deep", "data", "C:/outside"} {
		if _, err := svc.Tree(ctx, path); err == nil {
			t.Errorf("invalid tree path accepted: %s", path)
		}
	}
	if err := os.MkdirAll(filepath.Join(dir, "media/lyrics/artist/album"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "media/lyrics/artist/album/song.lrc"), []byte("[00:00]song"), 0644); err != nil {
		t.Fatal(err)
	}
	tree, err = svc.Tree(ctx, "lyrics/artist/album")
	if err != nil || len(tree.Items) != 1 || tree.Items[0].Hideable || tree.Items[0].Media {
		t.Fatalf("nested lyric tree: %+v %v", tree, err)
	}
}

func TestMediaAdminScopedSearchAliasesBoundsAndSymlinks(t *testing.T) {
	_, _, dir, public := publicFixture(t, true)
	svc := public.media
	ctx := context.Background()
	for _, name := range []string{"暗湧 黃耀明.mp3", "重庆音乐.mp3"} {
		if err := os.WriteFile(filepath.Join(dir, "media/music/artist", name), []byte("0123"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	for _, query := range []string{"暗涌", "暗湧", "anyong", "huangyaoming", "chongqing"} {
		value, err := svc.Search(ctx, query, "music")
		if err != nil || len(value.Items) != 1 {
			t.Fatalf("search %q: %+v %v", query, value, err)
		}
	}
	if _, err := svc.Search(ctx, "abc", ""); !errors.Is(err, media.ErrPath) {
		t.Fatalf("global search allowed: %v", err)
	}
	if _, err := svc.Search(ctx, "#_+", "music"); !errors.Is(err, search.ErrQuery) {
		t.Fatalf("punctuation query accepted: %v", err)
	}
	value, err := svc.Search(ctx, "chongqing", "music/nested")
	if err != nil || len(value.Items) != 0 {
		t.Fatalf("search escaped scope: %+v %v", value, err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "media/music/.hidden"), 0755); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(dir, "media/music/.hidden/暗湧.mp3"), []byte("0123"), 0644)
	if err := os.Symlink(filepath.Join(dir, "media/music/artist"), filepath.Join(dir, "media/music/link")); err == nil {
		value, err := svc.Search(ctx, "anyong", "music")
		if err != nil || len(value.Items) != 1 {
			t.Fatalf("symlink followed: %+v %v", value, err)
		}
	}
	for i := range 201 {
		if err := os.WriteFile(filepath.Join(dir, "media/music/artist", fmt.Sprintf("bounded%03d.mp3", i)), []byte("0123"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	value, err = svc.Search(ctx, "bounded", "music/artist")
	if err != nil || len(value.Items) != 200 || !value.Truncated {
		t.Fatalf("search bounds: %d %v %v", len(value.Items), value.Truncated, err)
	}
	canceled, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := svc.Search(canceled, "bounded", "music"); err == nil {
		t.Fatal("cancellation ignored")
	}
}
