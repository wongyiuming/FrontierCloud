package media

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func deleteFixture(t *testing.T) (string, *sqlitestore.Store, *Service) {
	t.Helper()
	root := t.TempDir()
	for _, name := range []string{"music/artist", "music/other", "vido/director", "lyrics/artist"} {
		if err := os.MkdirAll(filepath.Join(root, name), 0755); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{"music/artist/song.mp3", "music/other/song.mp3", "vido/director/video.mp4", "lyrics/artist/song.lrc"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("ID3payload"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	db, err := sqlitestore.Open(filepath.Join(t.TempDir(), "data.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := db.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	svc, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { svc.Close() })
	return root, db, svc
}

func TestDeleteDeduplicatesAndRemovesMetadataAtomically(t *testing.T) {
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	repo := db.Media()
	track := store.MediaObject{Path: "music/artist/song.mp3", Kind: "audio"}
	lyric := store.MediaObject{Path: "lyrics/artist/song.lrc", Kind: "lyric"}
	if err := repo.BindLyric(ctx, track, lyric); err != nil {
		t.Fatal(err)
	}
	ids, err := repo.EnsureObjects(ctx, []store.MediaObject{track, {Path: "music/other/song.mp3", Kind: "audio"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := repo.RecordPlayback(ctx, track, "20512c3b-5340-4185-b76d-20402279482a"); err != nil {
		t.Fatal(err)
	}
	if err := svc.Hide(ctx, []string{"music/artist"}, true, store.AdminAudit{Action: "hide"}); err != nil {
		t.Fatal(err)
	}
	count, err := svc.Delete(ctx, []string{"music/artist/song.mp3", "music/artist", "music/artist"}, store.AdminAudit{Action: "delete"})
	if err != nil || count != 1 {
		t.Fatalf("delete: %d %v", count, err)
	}
	if _, err := os.Stat(filepath.Join(root, "music/artist")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("deleted directory remains")
	}
	if _, err := os.Stat(filepath.Join(root, "music/other/song.mp3")); err != nil {
		t.Fatal("unselected sibling removed")
	}
	object, err := repo.ObjectByID(ctx, ids[track.Path])
	if err != nil || object != nil {
		t.Fatalf("deleted object retained: %+v %v", object, err)
	}
	object, err = repo.ObjectByID(ctx, ids["music/other/song.mp3"])
	if err != nil || object == nil {
		t.Fatal("case sibling metadata removed")
	}
	for _, table := range []string{"media_playback_stats", "media_playback_events", "media_lyric_links", "media_delete_operations", "media_visibility"} {
		var count int
		if err := db.Database().QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("metadata %s: %d %v", table, count, err)
		}
	}
	for _, name := range []string{"lyrics", defaultLyric} {
		if _, err := svc.Delete(ctx, []string{name}, store.AdminAudit{Action: "delete"}); err == nil {
			t.Fatalf("system fallback deletion accepted: %s", name)
		}
	}
}

func TestDeleteAuditFailureRestoresBytesAndMetadata(t *testing.T) {
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	name := "music/artist/song.mp3"
	ids, err := db.Media().EnsureObjects(ctx, []store.MediaObject{{Path: name, Kind: "audio"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Database().Exec("CREATE TRIGGER reject_delete_commit BEFORE INSERT ON admin_audit_log WHEN NEW.action='delete' AND NEW.result='success' BEGIN SELECT RAISE(ABORT,'audit offline'); END"); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Delete(ctx, []string{name}, store.AdminAudit{Action: "delete"}); err == nil {
		t.Fatal("audit failure accepted")
	}
	if payload, err := os.ReadFile(filepath.Join(root, name)); err != nil || string(payload) != "ID3payload" {
		t.Fatalf("file not restored: %s %v", payload, err)
	}
	object, err := db.Media().ObjectByID(ctx, ids[name])
	if err != nil || object == nil {
		t.Fatal("metadata not rolled back")
	}
	operations, err := db.Media().DeleteOperations(ctx)
	if err != nil || len(operations) != 0 {
		t.Fatalf("rollback left journal: %+v %v", operations, err)
	}
	if _, err := svc.Tree(ctx, "music/artist"); err != nil {
		t.Fatal("successful rollback blocked reads")
	}
}

func TestRestartRecoversPythonCompatiblePartialPendingDelete(t *testing.T) {
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	operation := store.DeleteOperation{ID: strings.Repeat("a", 32), State: "pending", Items: []store.DeleteItem{{Path: "music/artist/song.mp3", Slot: "0"}, {Path: "vido/director/video.mp4", Slot: "1"}}}
	if err := db.Media().PrepareDelete(ctx, operation, store.AdminAudit{Action: "delete"}); err != nil {
		t.Fatal(err)
	}
	quarantine := filepath.Join(root, ".delete-"+operation.ID)
	if err := os.Mkdir(quarantine, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(filepath.Join(root, operation.Items[0].Path), filepath.Join(quarantine, "0")); err != nil {
		t.Fatal(err)
	}
	svc.Close()
	recovered, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.Close()
	for _, item := range operation.Items {
		if _, err := os.Stat(filepath.Join(root, item.Path)); err != nil {
			t.Fatal("partial pending deletion not restored", err)
		}
	}
	if _, err := os.Stat(quarantine); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("pending quarantine remains")
	}
	journal, err := db.Media().DeleteOperations(ctx)
	if err != nil || len(journal) != 0 {
		t.Fatalf("journal remains: %v %v", journal, err)
	}
}

type ambiguousDeleteRepository struct {
	store.MediaRepository
	unreachable bool
}

func (r *ambiguousDeleteRepository) CommitDelete(ctx context.Context, id string, a store.AdminAudit) error {
	if err := r.MediaRepository.CommitDelete(ctx, id, a); err != nil {
		return err
	}
	r.unreachable = true
	return errors.New("ambiguous commit response")
}
func (r *ambiguousDeleteRepository) DeleteOperation(ctx context.Context, id string) (*store.DeleteOperation, error) {
	if r.unreachable {
		return nil, errors.New("database unavailable")
	}
	return r.MediaRepository.DeleteOperation(ctx, id)
}

func TestAmbiguousCommitBlocksMediaUntilRestartRecovery(t *testing.T) {
	root, db, original := deleteFixture(t)
	original.Close()
	fault := &ambiguousDeleteRepository{MediaRepository: db.Media()}
	svc, err := New(root, fault, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()
	if _, err := svc.Delete(context.Background(), []string{"music/artist"}, store.AdminAudit{Action: "delete"}); !errors.Is(err, ErrRecovery) {
		t.Fatalf("ambiguous commit not blocked: %v", err)
	}
	if _, err := svc.Tree(context.Background(), "music"); !errors.Is(err, ErrRecovery) {
		t.Fatal("read ignored recovery barrier")
	}
	if err := svc.Hide(context.Background(), []string{"vido/director"}, true, store.AdminAudit{Action: "hide"}); !errors.Is(err, ErrRecovery) {
		t.Fatal("mutation ignored recovery barrier")
	}
	recovered, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.Close()
	operations, err := db.Media().DeleteOperations(context.Background())
	if err != nil || len(operations) != 0 {
		t.Fatal("committed recovery incomplete")
	}
	if _, err := os.Stat(filepath.Join(root, "music/artist")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("committed deletion rolled back")
	}
}

func TestInvalidRecoveryManifestFailsClosed(t *testing.T) {
	root, db, svc := deleteFixture(t)
	svc.Close()
	if _, err := db.Database().Exec("INSERT INTO media_delete_operations (operation_id,state,manifest,created_at) VALUES (?,'pending',?,'2026-01-01')", strings.Repeat("b", 32), `[{"relative_path":"../outside","slot":"0","is_directory":true}]`); err != nil {
		t.Fatal(err)
	}
	if recovered, err := New(root, db.Media(), nil); err == nil {
		recovered.Close()
		t.Fatal("invalid recovery manifest accepted")
	}
	if _, err := os.Stat(filepath.Join(root, "music/artist/song.mp3")); err != nil {
		t.Fatal("invalid recovery removed unrelated bytes")
	}
}
