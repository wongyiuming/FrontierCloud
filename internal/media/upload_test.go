package media

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func staged(t *testing.T, svc *Service, payload string) *Stage {
	t.Helper()
	value, err := svc.Stage(context.Background(), strings.NewReader(payload), 1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(value.Close)
	return value
}

func TestUploadPublishesSimplifiedFilenameAndStableIdentity(t *testing.T) {
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	value := staged(t, svc, "ID3payload")
	name, err := svc.Publish(ctx, value, "暗湧.mp3", "music/artist", "", false, 240, store.AdminAudit{RequestID: "upload-test"})
	if err != nil || name != "music/artist/暗涌.mp3" {
		t.Fatalf("publish: %s %v", name, err)
	}
	if payload, err := os.ReadFile(filepath.Join(root, name)); err != nil || string(payload) != "ID3payload" {
		t.Fatalf("published payload: %s %v", payload, err)
	}
	ids, err := db.Media().EnsureObjects(ctx, []store.MediaObject{{Path: name, Kind: "audio"}})
	if err != nil || len(ids[name]) != 64 {
		t.Fatal("stable object missing", err)
	}
	for _, name := range []string{value.name, journalPath(value.id)} {
		if _, err := os.Stat(filepath.Join(root, name)); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("completed upload left recovery files", name)
		}
	}
	if err := db.Media().CompleteUpload(ctx, store.MediaObject{Path: name, Kind: "audio"}, value.id, store.AdminAudit{Action: "upload_item", RequestID: "upload-test"}); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE request_id=?", "upload-test").Scan(&count); err != nil || count != 1 {
		t.Fatalf("replay duplicated audit: %d %v", count, err)
	}
	duplicate := staged(t, svc, "ID3different")
	if _, err := svc.Publish(ctx, duplicate, "暗湧.mp3", "music/artist", "", false, 240, store.AdminAudit{}); !errors.Is(err, os.ErrExist) {
		t.Fatalf("duplicate overwrote media: %v", err)
	}
	if payload, err := os.ReadFile(filepath.Join(root, name)); err != nil || string(payload) != "ID3payload" {
		t.Fatal("duplicate changed payload")
	}
}

func TestUploadSignaturesLayoutAndLyricBoundaries(t *testing.T) {
	root, _, svc := deleteFixture(t)
	ctx := context.Background()
	for _, test := range []struct {
		filename, target, relative, payload string
		lyric                               bool
		want                                error
	}{
		{"bad.mp3", "music/artist", "", "not media", false, ErrSignature},
		{"song.mp3", "music/artist", "album/song.mp3", "ID3payload", false, ErrLayout},
		{"wrong.mp4", "music/artist", "", "0000ftyp0000", false, ErrPath},
		{"hidden.mp3", "music/artist", ".secret/song.mp3", "ID3payload", false, ErrPath},
		{"default.lrc", "", "", "[00:01]fallback", true, ErrPath},
		{"bad.lrc", "", "", "not lyrics", true, ErrSignature},
		{"bad.mp3", "music/artist", "../../bad.mp3", "ID3payload", false, ErrPath},
	} {
		value := staged(t, svc, test.payload)
		_, err := svc.Publish(ctx, value, test.filename, test.target, test.relative, test.lyric, 240, store.AdminAudit{})
		value.Close()
		if !errors.Is(err, test.want) {
			t.Errorf("%s (%s): %v != %v", test.filename, test.relative, err, test.want)
		}
	}
	if err := os.Mkdir(filepath.Join(root, "music/nested"), 0755); err != nil {
		t.Fatal(err)
	}
	value := staged(t, svc, "ID3payload")
	if name, err := svc.Publish(ctx, value, "song.mp3", "music/nested", "album/song.mp3", false, 240, store.AdminAudit{}); err != nil || name != "music/nested/album/song.mp3" {
		t.Fatalf("nested upload: %s %v", name, err)
	}
	flat := staged(t, svc, "ID3payload")
	if _, err := svc.Publish(ctx, flat, "flat.mp3", "music/nested", "", false, 240, store.AdminAudit{}); !errors.Is(err, ErrLayout) {
		t.Fatalf("flat/nested layout mixed: %v", err)
	}
	lyric := staged(t, svc, "[00:01]暗涌\n")
	if name, err := svc.Publish(ctx, lyric, "ignored.lrc", "", "artist/album/暗湧.lrc", true, 240, store.AdminAudit{}); err != nil || name != "lyrics/artist/album/暗涌.lrc" {
		t.Fatalf("nested lyric: %s %v", name, err)
	}
}

type boundedUploadReader struct {
	left    int64
	first   bool
	maximum int
}

func (r *boundedUploadReader) Read(value []byte) (int, error) {
	if len(value) > r.maximum {
		r.maximum = len(value)
	}
	if r.left == 0 {
		return 0, io.EOF
	}
	n := min(len(value), int(r.left))
	clear(value[:n])
	if !r.first {
		copy(value[:n], "ID3")
		r.first = true
	}
	r.left -= int64(n)
	return n, nil
}

func TestUploadStagesWithBoundedBufferAndCleansFailures(t *testing.T) {
	root, _, svc := deleteFixture(t)
	reader := &boundedUploadReader{left: 8 * 1024 * 1024}
	stage, err := svc.Stage(context.Background(), reader, 8*1024*1024)
	if err != nil || stage.Bytes != 8*1024*1024 || reader.maximum > 64*1024 {
		t.Fatalf("bounded stage: %+v %d %v", stage, reader.maximum, err)
	}
	stage.Close()
	if _, err := svc.Stage(context.Background(), bytes.NewReader(make([]byte, 101)), 100); !errors.Is(err, ErrUploadSize) {
		t.Fatalf("size limit ignored: %v", err)
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := svc.Stage(canceled, strings.NewReader("ID3payload"), 100); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation ignored: %v", err)
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if uploadStageName.MatchString(entry.Name()) {
			t.Fatal("failed upload left stage")
		}
	}
}

type uncertainUploadRepository struct{ store.MediaRepository }

func (r *uncertainUploadRepository) CompleteUpload(ctx context.Context, object store.MediaObject, id string, a store.AdminAudit) error {
	if err := r.MediaRepository.CompleteUpload(ctx, object, id, a); err != nil {
		return err
	}
	return errors.New("ambiguous commit response")
}

func TestUploadAmbiguousCommitRecoversWithoutDuplicateAudit(t *testing.T) {
	root, db, original := deleteFixture(t)
	original.Close()
	svc, err := New(root, &uncertainUploadRepository{db.Media()}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer svc.Close()
	value := staged(t, svc, "ID3payload")
	name := "music/artist/recovery.mp3"
	if _, err := svc.Publish(context.Background(), value, "recovery.mp3", "music/artist", "", false, 240, store.AdminAudit{RequestID: "recover-upload"}); !errors.Is(err, ErrRecovery) {
		t.Fatalf("ambiguous upload not blocked: %v", err)
	}
	if _, err := svc.Tree(context.Background(), "music"); !errors.Is(err, ErrRecovery) {
		t.Fatal("read ignored upload recovery barrier")
	}
	value.Close()
	if _, err := os.Stat(filepath.Join(root, journalPath(value.id))); err != nil {
		t.Fatal("replay intent lost", err)
	}
	recovered, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.Close()
	if payload, err := os.ReadFile(filepath.Join(root, name)); err != nil || string(payload) != "ID3payload" {
		t.Fatal("recovery lost uploaded media")
	}
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE request_id=?", "recover-upload").Scan(&count); err != nil || count != 1 {
		t.Fatalf("recovery duplicate audit: %d %v", count, err)
	}
	if _, err := os.Stat(filepath.Join(root, journalPath(value.id))); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("recovery did not clear journal")
	}
}

func TestUploadRecoversPrePublicationIntentAndRejectsTampering(t *testing.T) {
	for _, tamper := range []bool{false, true} {
		t.Run(map[bool]string{false: "resume", true: "tamper"}[tamper], func(t *testing.T) {
			root, db, svc := deleteFixture(t)
			value := staged(t, svc, "ID3payload")
			value.retain = true
			journal := uploadJournal{Format: "frontiercloud-local-upload", Version: 1, ID: value.id, Object: store.MediaObject{Path: "music/new/album/song.mp3", Kind: "audio"}, Bytes: value.Bytes, SHA256: value.Digest, Audit: store.AdminAudit{Action: "upload_item"}}
			if err := svc.writeUploadJournal(journal); err != nil {
				t.Fatal(err)
			}
			if tamper {
				if err := os.WriteFile(filepath.Join(root, value.name), []byte("BADpayload"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			svc.Close()
			recovered, err := New(root, db.Media(), nil)
			if tamper {
				if err == nil {
					recovered.Close()
					t.Fatal("tampered stage published")
				}
				if _, err := os.Stat(filepath.Join(root, journal.Object.Path)); !errors.Is(err, os.ErrNotExist) {
					t.Fatal("tampered stage created destination")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			defer recovered.Close()
			if payload, err := os.ReadFile(filepath.Join(root, journal.Object.Path)); err != nil || string(payload) != "ID3payload" {
				t.Fatal("pre-publication recovery failed")
			}
		})
	}
}
