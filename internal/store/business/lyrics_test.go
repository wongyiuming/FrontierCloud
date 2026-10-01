package business_test

import (
	"context"
	"database/sql"
	"sync"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestLyricRelationsAtomicReverseAndAutoPreservesExplicit(t *testing.T) {
	db := database(t)
	repo := db.Media()
	ctx := context.Background()
	base := "music/" + t.Name()
	tracks := []store.MediaObject{{Path: base + "/a.mp3", Kind: "audio"}, {Path: base + "/b.mp3", Kind: "audio"}}
	lyric := store.MediaObject{Path: "lyrics/" + t.Name() + ".lrc", Kind: "lyric"}
	fallback := store.MediaObject{Path: "lyrics/default.lrc", Kind: "lyric"}
	count, err := repo.ReplaceLyricRelations(ctx, lyric, tracks, fallback, store.AdminAudit{})
	if err != nil || count != 2 {
		t.Fatalf("reverse: %d %v", count, err)
	}
	other := store.MediaObject{Path: "lyrics/" + t.Name() + "-other.lrc", Kind: "lyric"}
	pairs := []store.LyricPair{{Track: tracks[0], Lyric: other}, {Track: tracks[1], Lyric: other}}
	result, err := repo.AutoLyricRelations(ctx, pairs, store.AutoLyricResult{}, store.AdminAudit{})
	if err != nil || result.Preserved != 2 || result.Linked != 0 {
		t.Fatalf("explicit changed: %+v %v", result, err)
	}
	count, err = repo.ReplaceLyricRelations(ctx, lyric, tracks[:1], fallback, store.AdminAudit{})
	if err != nil || count != 1 {
		t.Fatalf("reverse removal: %d %v", count, err)
	}
	var workers sync.WaitGroup
	failures := make(chan error, 8)
	for range 8 {
		workers.Go(func() {
			if _, err := repo.AutoLyricRelations(ctx, pairs, store.AutoLyricResult{}, store.AdminAudit{}); err != nil {
				failures <- err
			}
		})
	}
	workers.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	ids, err := repo.EnsureObjects(ctx, tracks)
	if err != nil {
		t.Fatal(err)
	}
	for i, track := range tracks {
		got, err := repo.LyricPath(ctx, ids[track.Path])
		want := lyric.Path
		if i == 1 {
			want = other.Path
		}
		if err != nil || got != want {
			t.Fatalf("relation %d: %s %v", i, got, err)
		}
	}
	relations, err := repo.LyricRelations(ctx, base, "lyrics")
	if err != nil || len(relations) != 2 {
		t.Fatalf("catalog: %v %v", relations, err)
	}
	if db.Backend() != "sqlite" {
		return
	}
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	if _, err := sqlDB.Exec("CREATE TRIGGER lyric_audit_fault BEFORE INSERT ON admin_audit_log BEGIN SELECT RAISE(FAIL,'audit unavailable'); END"); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.ReplaceLyricRelations(ctx, tracks[0], []store.MediaObject{other}, fallback, store.AdminAudit{}); err == nil {
		t.Fatal("audit failure accepted")
	}
	got, err := repo.LyricPath(ctx, ids[tracks[0].Path])
	if err != nil || got != lyric.Path {
		t.Fatal("lyric mutation survived transaction rollback")
	}
}
