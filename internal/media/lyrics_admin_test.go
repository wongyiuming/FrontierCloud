package media

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestLyricCatalogHierarchyAndManualAutoRelations(t *testing.T) {
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	for _, name := range []string{"music/artist/album/暗涌.mp3", "music/artist/album/unmatched.mp3", "music/artist/album/ambiguous.mp3", "lyrics/artist/album/暗涌.lrc", "lyrics/artist/album/ambiguous.lrc", "lyrics/other/ambiguous.lrc", "lyrics/manual.lrc"} {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(root, name)), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, name), []byte("[00:01]歌词\n"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	// A matching path wins over ambiguous same-basename candidates.
	if count, err := svc.ReplaceLyrics(ctx, "track", "music/artist/album/暗涌.mp3", []string{"lyrics/manual.lrc"}, store.AdminAudit{}); err != nil || count != 1 {
		t.Fatalf("manual: %d %v", count, err)
	}
	result, err := svc.AutoLyrics(ctx, store.AdminAudit{})
	if err != nil || result.Preserved != 1 || result.Linked != 3 || result.Unmatched != 1 || result.Ambiguous != 0 {
		t.Fatalf("auto: %+v %v", result, err)
	}
	// Explicit business choice survives even if its physical file disappears.
	if err := os.Remove(filepath.Join(root, "lyrics/manual.lrc")); err != nil {
		t.Fatal(err)
	}
	result, err = svc.AutoLyrics(ctx, store.AdminAudit{})
	if err != nil || result.Preserved != 4 {
		t.Fatalf("missing manual overwritten: %+v %v", result, err)
	}
	catalog, err := svc.LyricCatalog(ctx, "music/artist", "lyrics/artist", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(catalog.TrackDirectories) != 1 || catalog.TrackDirectories[0].Count != 3 || catalog.Counts["tracks"] != 4 || catalog.Counts["lyrics"] != 3 {
		t.Fatalf("hierarchy/counts: %+v", catalog)
	}
	query, err := svc.LyricCatalog(ctx, "music", "lyrics", "anyong", "anyong")
	if err != nil || len(query.Tracks) != 1 || len(query.Lyrics) != 1 || len(query.TrackDirectories) != 0 || query.Tracks[0].LyricPath == nil || *query.Tracks[0].LyricPath != "lyrics/manual.lrc" {
		t.Fatalf("search/explicit link: %+v %v", query, err)
	}
	if _, err := svc.ReplaceLyrics(ctx, "track", "music/artist/album/unmatched.mp3", []string{}, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	unmatched, err := svc.LyricCatalog(ctx, "music/artist/album", "lyrics", "", "")
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(unmatched)
	if !strings.Contains(string(encoded), `"lyric_path":null`) || strings.Contains(string(encoded), defaultLyric) {
		t.Fatalf("system fallback exposed: %s", encoded)
	}
	if count, err := svc.ReplaceLyrics(ctx, "lyric", "lyrics/artist/song.lrc", []string{"music/artist/album/unmatched.mp3"}, store.AdminAudit{}); err != nil || count != 1 {
		t.Fatalf("replace reverse: %d %v", count, err)
	}
	ids, err := db.Media().EnsureObjects(ctx, []store.MediaObject{{Path: "music/artist/song.mp3", Kind: "audio"}, {Path: "music/other/song.mp3", Kind: "audio"}})
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range ids {
		lyric, err := db.Media().LyricPath(ctx, id)
		if err != nil || lyric != defaultLyric {
			t.Fatalf("removed reverse relation not fallback: %s %v", lyric, err)
		}
	}
	for _, scope := range []string{"", "vido", "music/artist/album/deeper", "../outside"} {
		if _, err := svc.LyricCatalog(ctx, scope, "lyrics", "", ""); err == nil {
			t.Fatalf("invalid scope accepted %q", scope)
		}
	}
	if _, err := svc.ReplaceLyrics(ctx, "lyric", defaultLyric, []string{}, store.AdminAudit{}); err == nil {
		t.Fatal("default lyric directly mutated")
	}
}
func TestAutoLyricsAmbiguityAndInvalidTargetAllOrNothing(t *testing.T) {
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	if err := os.Remove(filepath.Join(root, "lyrics/artist/song.lrc")); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"lyrics/one/song.lrc", "lyrics/two/song.lrc", "lyrics/default.lrc/invalid.lrc"} {
		if strings.Contains(name, "default.lrc/") {
			continue
		}
		os.MkdirAll(filepath.Dir(filepath.Join(root, name)), 0755)
		if err := os.WriteFile(filepath.Join(root, name), []byte("[00:01]歌词"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	result, err := svc.AutoLyrics(ctx, store.AdminAudit{})
	if err != nil || result.Ambiguous != 2 || result.Linked != 0 {
		t.Fatalf("ambiguity: %+v %v", result, err)
	}
	if _, err := svc.ReplaceLyrics(ctx, "lyric", "lyrics/one/song.lrc", []string{"music/artist/song.mp3", "music/missing/file.mp3"}, store.AdminAudit{}); err == nil {
		t.Fatal("invalid partial target accepted")
	}
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM media_lyric_links").Scan(&count); err != nil || count != 0 {
		t.Fatalf("partial relation mutation: %d %v", count, err)
	}
}
