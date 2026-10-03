package recording

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func storageFixture(t *testing.T) (*Storage, *sqlite.Store, store.KaraokeUser, string) {
	t.Helper()
	dir := t.TempDir()
	db, e := sqlite.Open(filepath.Join(dir, "db.sqlite"))
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	ctx := context.Background()
	if e = db.Initialize(ctx); e != nil {
		t.Fatal(e)
	}
	id := strings.Repeat("a", 32)
	if _, e = db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: id, Role: "Standalone", PrivateKey: "fixture", CreatedAt: time.Now().Unix()}); e != nil {
		t.Fatal(e)
	}
	if _, e = db.Nodes().PromoteIdentity(ctx, store.NodePromotion{Role: "Master", Endpoint: "https://recording.test", Allocation: 5 * store.GiB, PhysicalFree: 10 * store.GiB}, store.NodeAudit{}); e != nil {
		t.Fatal(e)
	}
	u := store.KaraokeUser{ID: strings.Repeat("b", 32), Username: "storage-user", NameKey: "storage-user", PasswordHash: "fixture", Quota: 1024 * 1024}
	if e = db.Karaoke().RegisterUser(ctx, u, "192.0.2.179", "20261001", store.KaraokeAudit{}); e != nil {
		t.Fatal(e)
	}
	if e = os.Mkdir(filepath.Join(dir, "recordings"), 0700); e != nil {
		t.Fatal(e)
	}
	root, e := os.OpenRoot(filepath.Join(dir, "recordings"))
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { root.Close() })
	s, e := New(root, db.Recordings(), db.Nodes())
	if e != nil {
		t.Fatal(e)
	}
	return s, db, u, id
}
func reserve(t *testing.T, s *Storage, db *sqlite.Store, user, id string, size int64) store.Recording {
	t.Helper()
	v, _, e := db.Recordings().ReserveRecording(context.Background(), store.Recording{ID: id, UserID: user, Filename: "录音.webm", ContentType: "audio/webm", Bytes: size}, 10*store.GiB, store.KaraokeAudit{})
	if e != nil {
		t.Fatal(e)
	}
	return v
}

type deletionFault struct {
	store.RecordingRepository
	fail, lost bool
}

func (r *deletionFault) CompleteRecordingDeletion(ctx context.Context, user, id string, a store.KaraokeAudit) error {
	if r.fail {
		return errors.New("rollback fixture")
	}
	e := r.RecordingRepository.CompleteRecordingDeletion(ctx, user, id, a)
	if e == nil && r.lost {
		return errors.New("commit acknowledgement lost")
	}
	return e
}
func TestPrivateStorageUploadSizeFooterReplayAndDeletionRecovery(t *testing.T) {
	s, db, u, relationship := storageFixture(t)
	ctx := context.Background()
	id := strings.Repeat("c", 32)
	data := trailer(`{"version":1,"title":"录制现场","lyrics":[{"time":1.5,"text":"前沿"}]}`)
	v := reserve(t, s, db, u.ID, id, int64(len(data)))
	unlock, e := s.Lock(id)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.Upload(ctx, relationship, v, bytes.NewReader(data[:len(data)-1]), store.NodeAudit{}); !errors.Is(e, ErrSize) {
		t.Fatal("short upload", e)
	}
	if _, e = s.root.Lstat(".recording-" + id + ".part"); !errors.Is(e, os.ErrNotExist) {
		t.Fatal("stage not removed", e)
	}
	receipt, e := s.Upload(ctx, relationship, v, bytes.NewReader(data), store.NodeAudit{})
	if e != nil || receipt.Metadata == nil || receipt.Metadata.Title != "录制现场" {
		t.Fatal(receipt, e)
	}
	// Replayed requests cannot replace an existing recording with new bytes.
	replayed, e := s.Upload(ctx, relationship, v, strings.NewReader(strings.Repeat("x", len(data))), store.NodeAudit{})
	if e != nil || replayed.SHA256 != receipt.SHA256 {
		t.Fatal("overwrite on replay", replayed, e)
	}
	unlock()
	row, e := db.Recordings().Recording(ctx, id)
	if e != nil || row.State != "pending" || row.SHA256 != nil {
		t.Fatal("raw upload finalized business quota", row, e)
	}
	if e = db.Recordings().FinalizeRecording(ctx, u.ID, id, receipt, store.KaraokeAudit{}); e != nil {
		t.Fatal(e)
	}
	name, _ := Path(relationship, u.ID, id)
	if runtime.GOOS != "windows" {
		published, err := s.root.Stat(name)
		private, privateErr := s.root.Stat(".recordings-mutation.lock")
		if err != nil || privateErr != nil || published.Mode().Perm() != 0640 || private.Mode().Perm() != 0600 {
			t.Fatal("finalized recording read group or private lease permissions changed", err, privateErr)
		}
	}
	file, info, release, e := s.Open(ctx, relationship, u.ID, id)
	if e != nil || info.Size() != int64(len(data)) {
		t.Fatal(info, e)
	}
	read, e := io.ReadAll(file)
	release()
	if e != nil || !bytes.Equal(read, data) {
		t.Fatal("stored bytes", e)
	}
	if _, e = db.Recordings().StageRecordingDeletion(ctx, u.ID, id, false, store.KaraokeAudit{}); e != nil {
		t.Fatal(e)
	}
	fault := &deletionFault{RecordingRepository: db.Recordings(), fail: true}
	s.repo = fault
	unlock, _ = s.Lock(id)
	e = s.Delete(ctx, relationship, u.ID, id, store.KaraokeAudit{}, store.NodeAudit{})
	unlock()
	if e == nil {
		t.Fatal("rollback missing")
	}
	if _, e = s.info(name); e != nil {
		t.Fatal("physical rollback not restored", e)
	}
	row, e = db.Recordings().Recording(ctx, id)
	if e != nil || row.State != "deleting" {
		t.Fatal(row, e)
	}
	if !errors.Is(s.Ready(ctx), ErrRecovery) {
		t.Fatal("unreconciled volume not fenced")
	}
	fault.fail = false
	if e = s.Recover(ctx); e != nil {
		t.Fatal(e)
	}
	fault.lost = true
	unlock, _ = s.Lock(id)
	e = s.Delete(ctx, relationship, u.ID, id, store.KaraokeAudit{}, store.NodeAudit{})
	unlock()
	if e != nil {
		t.Fatal("lost commit not reconciled", e)
	}
	row, e = db.Recordings().Recording(ctx, id)
	if e != nil || row != nil {
		t.Fatal(row, e)
	}
	account, e := db.Karaoke().UserByID(ctx, u.ID)
	if e != nil || account.Used != 0 {
		t.Fatal("refund", account, e)
	}
	if _, e = s.info(name); !errors.Is(e, os.ErrNotExist) {
		t.Fatal("committed bytes", e)
	}
	if e = s.Recover(ctx); e != nil {
		t.Fatal(e)
	}
}
func TestPrivateStorageCrashPublicationAndDurableDeleteJournal(t *testing.T) {
	for _, phase := range []string{"before-publication", "after-publication", "before-delete-commit", "after-delete-commit", "missing-physical"} {
		t.Run(phase, func(t *testing.T) {
			s, db, u, rel := storageFixture(t)
			ctx := context.Background()
			id := strings.Repeat("d", 32)
			data := []byte("opaque binary recording")
			v := reserve(t, s, db, u.ID, id, int64(len(data)))
			hash := sha256.Sum256(data)
			receipt := store.RecordingReceipt{ID: id, Bytes: int64(len(data)), SHA256: hex.EncodeToString(hash[:])}
			name, _ := Path(rel, u.ID, id)
			stage := ".recording-" + id + ".part"
			if e := s.root.WriteFile(stage, data, 0600); e != nil {
				t.Fatal(e)
			}
			j := journal{Version: 1, Operation: "upload", Relationship: rel, Recording: v, UserID: u.ID, MemberID: v.MemberID, Receipt: receipt}
			if e := s.writeJournal(j); e != nil {
				t.Fatal(e)
			}
			if phase != "before-publication" {
				if e := s.makeParents(name); e != nil {
					t.Fatal(e)
				}
				if e := s.root.Link(stage, name); e != nil {
					t.Fatal(e)
				}
			}
			if e := s.Recover(ctx); e != nil {
				t.Fatal(e)
			}
			actual, e := s.Stat(ctx, rel, u.ID, id)
			if e != nil || actual.SHA256 != receipt.SHA256 {
				t.Fatal(actual, e)
			}
			if e := db.Recordings().FinalizeRecording(ctx, u.ID, id, receipt, store.KaraokeAudit{}); e != nil {
				t.Fatal(e)
			}
			if strings.Contains(phase, "delete") || phase == "missing-physical" {
				v, e := db.Recordings().StageRecordingDeletion(ctx, u.ID, id, false, store.KaraokeAudit{})
				if e != nil {
					t.Fatal(e)
				}
				j = journal{Version: 1, Operation: "delete", Relationship: rel, Recording: *v, UserID: u.ID, MemberID: v.MemberID}
				if phase == "missing-physical" {
					if e = s.root.Remove(name); e != nil {
						t.Fatal(e)
					}
					j.Absent = true
				}
				if e = s.writeJournal(j); e != nil {
					t.Fatal(e)
				}
				if !j.Absent {
					if e = s.root.Rename(name, ".recording-"+id+".deleted"); e != nil {
						t.Fatal(e)
					}
				}
				if phase == "after-delete-commit" {
					if e = db.Recordings().CompleteRecordingDeletion(ctx, u.ID, id, store.KaraokeAudit{}); e != nil {
						t.Fatal(e)
					}
				}
				if e = s.Recover(ctx); e != nil {
					t.Fatal(e)
				}
				if _, e = s.Stat(ctx, rel, u.ID, id); !errors.Is(e, store.ErrRecordingMissing) {
					t.Fatal("deleted recording resurrected", e)
				}
				u, e := db.Karaoke().UserByID(ctx, u.ID)
				if e != nil || u.Used != 0 {
					t.Fatal(u, e)
				}
			}
		})
	}
}
func TestPrivateStorageRecoveryDoesNotRemoveLiveStageAndRejectsSymlinks(t *testing.T) {
	s, db, u, rel := storageFixture(t)
	ctx := context.Background()
	id := strings.Repeat("e", 32)
	unlock, e := s.Lock(id)
	if e != nil {
		t.Fatal(e)
	}
	stage := ".recording-" + id + ".part"
	if e = s.root.WriteFile(stage, []byte("live"), 0600); e != nil {
		t.Fatal(e)
	}
	if e = s.Recover(ctx); e != nil {
		t.Fatal(e)
	}
	if _, e = s.info(stage); e != nil {
		t.Fatal("live stage removed", e)
	}
	if _, e = s.Lock(id); !errors.Is(e, filelease.ErrBusy) {
		t.Fatal("recording lease not exclusive", e)
	}
	unlock()
	if e = s.Recover(ctx); e != nil {
		t.Fatal(e)
	}
	if _, e = s.root.Lstat(stage); !errors.Is(e, os.ErrNotExist) {
		t.Fatal("abandoned stage", e)
	}
	if e = os.Symlink(t.TempDir(), filepath.Join(s.root.Name(), rel)); e != nil {
		t.Skip("symlink privilege unavailable")
	}
	v := reserve(t, s, db, u.ID, id, 4)
	unlock, _ = s.Lock(id)
	defer unlock()
	if _, e = s.Upload(ctx, rel, v, strings.NewReader("data"), store.NodeAudit{}); !errors.Is(e, ErrRecovery) {
		t.Fatal("symlink traversed", e)
	}
}
func TestPrivateStorageBusyIntentRetainsReadinessFence(t *testing.T) {
	s, db, u, rel := storageFixture(t)
	ctx := context.Background()
	id := strings.Repeat("f", 32)
	data := []byte("data")
	v := reserve(t, s, db, u.ID, id, 4)
	hash := sha256.Sum256(data)
	receipt := store.RecordingReceipt{ID: id, Bytes: 4, SHA256: hex.EncodeToString(hash[:])}
	if e := s.root.WriteFile(".recording-"+id+".part", data, 0600); e != nil {
		t.Fatal(e)
	}
	if e := s.writeJournal(journal{Version: 1, Operation: "upload", Relationship: rel, Recording: v, UserID: u.ID, MemberID: v.MemberID, Receipt: receipt}); e != nil {
		t.Fatal(e)
	}
	unlock, e := s.Lock(id)
	if e != nil {
		t.Fatal(e)
	}
	s.fence()
	if e = s.Recover(ctx); !errors.Is(e, ErrRecovery) {
		t.Fatal("busy intent readiness restored", e)
	}
	if e = s.Ready(ctx); !errors.Is(e, ErrRecovery) {
		t.Fatal("busy intent fence lost", e)
	}
	unlock()
	if e = s.Recover(ctx); e != nil {
		t.Fatal(e)
	}
	if e = s.Ready(ctx); e != nil {
		t.Fatal(e)
	}
}
