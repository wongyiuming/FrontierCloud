package media

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestRenameKeepsIdentityAndRecoversLostCommit(t *testing.T) {
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	track := store.MediaObject{Path: "music/artist/song.mp3", Kind: "audio"}
	ids, err := db.Media().EnsureObjects(ctx, []store.MediaObject{track})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Media().RecordPlayback(ctx, track, "20512c3b-5340-4185-b76d-20402279482a"); err != nil {
		t.Fatal(err)
	}
	if err := db.Media().BindLyric(ctx, track, store.MediaObject{Path: "lyrics/artist/song.lrc", Kind: "lyric"}); err != nil {
		t.Fatal(err)
	}
	if err := db.Media().SetHidden(ctx, []string{"music/artist"}, true, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	svc.repository = &lostRename{MediaRepository: db.Media()}
	if _, err := svc.Rename(ctx, "music/artist", "renamed", store.AdminAudit{RequestID: "rename-test"}); !errors.Is(err, ErrRecovery) {
		t.Fatalf("lost commit: %v", err)
	}
	if err := svc.Ready(ctx); !errors.Is(err, ErrRecovery) {
		t.Fatal("ambiguous rename allowed traffic")
	}
	if _, err := os.Stat(filepath.Join(root, "music/renamed/song.mp3")); err != nil {
		t.Fatal(err)
	}
	svc.Close()
	restarted, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer restarted.Close()
	o, err := db.Media().ObjectByID(ctx, ids[track.Path])
	if err != nil || o == nil || o.Path != "music/renamed/song.mp3" {
		t.Fatalf("identity lost: %+v %v", o, err)
	}
	stats, err := db.Media().Stats(ctx, []string{o.ID})
	if err != nil || stats[o.ID].PlayScore != 1 {
		t.Fatalf("stats lost: %v %v", stats, err)
	}
	hidden, err := db.Media().HiddenPaths(ctx)
	if err != nil || hidden["music/artist"] || !hidden["music/renamed"] {
		t.Fatalf("visibility: %v %v", hidden, err)
	}
	lyric, err := db.Media().LyricPath(ctx, o.ID)
	if err != nil || lyric != "lyrics/artist/song.lrc" {
		t.Fatal("lyric relation lost")
	}
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE request_id='rename-test'").Scan(&count); err != nil || count != 1 {
		t.Fatalf("duplicate audits: %d %v", count, err)
	}
	if _, err := os.Stat(filepath.Join(root, "music/renamed", renameMarker(strings.Repeat("a", 32)))); err == nil {
		t.Fatal("marker remains")
	}
	result, err := restarted.Rename(ctx, "music/renamed", "renamed", store.AdminAudit{})
	if err != nil || result.Status != "unchanged" {
		t.Fatalf("same name: %+v %v", result, err)
	}
}

type lostRename struct{ store.MediaRepository }

func (r *lostRename) CompleteRename(ctx context.Context, old, target, id string, a store.AdminAudit) error {
	if err := r.MediaRepository.CompleteRename(ctx, old, target, id, a); err != nil {
		return err
	}
	return errors.New("lost commit acknowledgement")
}
func TestRenameRejectsTargetsAndInvalidNamesBeforeMoving(t *testing.T) {
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	for _, name := range []string{"", ".hidden", "../escape", "name/child", "name:drive", strings.Repeat("中", 86)} {
		if _, err := svc.Rename(ctx, "music/artist", name, store.AdminAudit{}); !errors.Is(err, ErrPath) {
			t.Fatalf("invalid name accepted %q: %v", name, err)
		}
	}
	if _, err := svc.Rename(ctx, "music/artist", "other", store.AdminAudit{}); !errors.Is(err, os.ErrExist) {
		t.Fatalf("existing target: %v", err)
	}
	if _, err := db.Media().EnsureObjects(ctx, []store.MediaObject{{Path: "music/ghost/song.mp3", Kind: "audio"}}); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Rename(ctx, "music/artist", "ghost", store.AdminAudit{}); !errors.Is(err, os.ErrExist) {
		t.Fatalf("stale file identity overwritten: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "music/artist/song.mp3")); err != nil {
		t.Fatal("source moved on preflight failure")
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".rename-") {
			t.Fatal("journal created for rejected rename")
		}
	}
}
func TestRenameRecoveryOwnsDestination(t *testing.T) {
	root, db, svc := deleteFixture(t)
	svc.Close()
	id := strings.Repeat("d", 32)
	journal := renameJournal{Format: "frontiercloud-local-rename", Version: 1, ID: id, Old: "music/missing", New: "music/other", Audit: store.AdminAudit{Action: "directory_rename"}}
	if err := svc.writeRenameJournal(journal); err == nil {
		t.Fatal("closed root accepted journal")
	}
	r, err := os.OpenRoot(root)
	if err != nil {
		t.Fatal(err)
	}
	service := &Service{root: r, repository: db.Media()}
	if err := service.writeRenameJournal(journal); err != nil {
		t.Fatal(err)
	}
	r.Close()
	if ready, err := New(root, db.Media(), nil); err == nil {
		ready.Close()
		t.Fatal("unowned destination replay accepted")
	}
	if _, err := os.Stat(filepath.Join(root, "music/other/song.mp3")); err != nil {
		t.Fatal("unowned data altered")
	}
}
