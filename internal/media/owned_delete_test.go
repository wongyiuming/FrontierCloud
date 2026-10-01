package media

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type ambiguousOwnedDeleteRepo struct {
	store.MediaRepository
	store.OwnedStorageRepository
}

func (r *ambiguousOwnedDeleteRepo) CommitOwnedDelete(ctx context.Context, id string, free int64, a store.NodeAudit) error {
	if err := r.OwnedStorageRepository.CommitOwnedDelete(ctx, id, free, a); err != nil {
		return err
	}
	return errors.New("lost delete acknowledgement")
}
func TestOwnedDeleteAtomicAuditRollbackLostResponseAndConcurrentRetry(t *testing.T) {
	for _, phase := range []string{"concurrent", "audit_failure", "lost_commit"} {
		t.Run(phase, func(t *testing.T) {
			root, db, svc, rel := ownedFixture(t)
			ctx := context.Background()
			object := store.MediaObject{ID: strings.Repeat("5", 64), Path: "music/OwnedDelete/song.mp3", Kind: "audio"}
			if _, err := svc.OwnedUpload(ctx, rel, object, 10, strings.NewReader("ID3payload"), store.NodeAudit{}); err != nil {
				t.Fatal(err)
			}
			if phase == "lost_commit" {
				svc.Close()
				fault, err := New(root, &ambiguousOwnedDeleteRepo{db.Media(), db.Pool()}, svc.identity)
				if err != nil {
					t.Fatal(err)
				}
				defer fault.Close()
				svc = fault
			}
			if phase == "audit_failure" {
				if _, err := db.Database().Exec("CREATE TRIGGER owned_delete_audit_failure BEFORE INSERT ON node_audit WHEN NEW.action='storage-delete-committed' BEGIN SELECT RAISE(FAIL,'audit unavailable'); END"); err != nil {
					t.Fatal(err)
				}
				if err := svc.OwnedDelete(ctx, rel, object.ID, object.Path, 10, store.NodeAudit{}); err == nil {
					t.Fatal("unaudited deletion committed")
				}
				ownedFunds(t, db, 10, 0)
				if payload, err := os.ReadFile(filepath.Join(root, object.Path)); err != nil || string(payload) != "ID3payload" {
					t.Fatal("failed delete did not restore bytes", err)
				}
				if _, err := db.Database().Exec("DROP TRIGGER owned_delete_audit_failure"); err != nil {
					t.Fatal(err)
				}
			}
			if err := svc.OwnedDelete(ctx, rel, object.ID, object.Path, 9, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
				t.Fatal("size mismatch deleted", err)
			}
			var group sync.WaitGroup
			for range 16 {
				group.Go(func() {
					if err := svc.OwnedDelete(ctx, rel, object.ID, object.Path, 10, store.NodeAudit{}); err != nil {
						t.Error(err)
					}
				})
			}
			group.Wait()
			ownedFunds(t, db, 0, 0)
			if _, err := os.Stat(filepath.Join(root, object.Path)); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("deleted bytes remain", err)
			}
			row, err := db.Media().ObjectByID(ctx, object.ID)
			if err != nil || row != nil {
				t.Fatal("deleted object remains", row, err)
			}
			var count int
			if err = db.Database().QueryRow("SELECT COUNT(*) FROM node_audit WHERE action='storage-delete-committed'").Scan(&count); err != nil || count != 1 {
				t.Fatal("duplicate delete quota/audit", count, err)
			}
			if _, err = svc.OwnedUpload(ctx, rel, store.MediaObject{ID: strings.Repeat("6", 64), Path: object.Path, Kind: "audio"}, 10, strings.NewReader("ID3payload"), store.NodeAudit{}); err != nil {
				t.Fatal("cleaned path cannot be reused", err)
			}
		})
	}
}
func TestOwnedDeleteInterruptedQuarantineRecoveryRestoresOnlyPending(t *testing.T) {
	for _, commit := range []bool{false, true} {
		t.Run(map[bool]string{false: "rollback", true: "committed_cleanup"}[commit], func(t *testing.T) {
			root, db, svc, rel := ownedFixture(t)
			ctx := context.Background()
			object := store.MediaObject{ID: strings.Repeat("7", 64), Path: "music/QuarantineOwned/song.mp3", Kind: "audio"}
			if _, err := svc.OwnedUpload(ctx, rel, object, 10, strings.NewReader("ID3payload"), store.NodeAudit{}); err != nil {
				t.Fatal(err)
			}
			operation := store.DeleteOperation{ID: strings.Repeat("8", 32), State: "pending", Items: []store.DeleteItem{{Path: object.Path, Slot: "0", OwnedID: object.ID, Bytes: 10}}}
			if err := db.Pool().PrepareOwnedDelete(ctx, rel, operation, store.NodeAudit{}); err != nil {
				t.Fatal(err)
			}
			if err := svc.root.Mkdir(".delete-"+operation.ID, 0700); err != nil {
				t.Fatal(err)
			}
			if err := svc.root.Rename(object.Path, ".delete-"+operation.ID+"/0"); err != nil {
				t.Fatal(err)
			}
			if commit {
				if err := db.Pool().CommitOwnedDelete(ctx, operation.ID, 10*store.GiB, store.NodeAudit{}); err != nil {
					t.Fatal(err)
				}
			}
			svc.Close()
			recovered, err := New(root, db.Media(), svc.identity)
			if err != nil {
				t.Fatal(err)
			}
			defer recovered.Close()
			if commit {
				ownedFunds(t, db, 0, 0)
				if _, err = os.Stat(filepath.Join(root, object.Path)); !errors.Is(err, os.ErrNotExist) {
					t.Fatal("committed file resurrected")
				}
			} else {
				ownedFunds(t, db, 10, 0)
				if _, err = os.Stat(filepath.Join(root, object.Path)); err != nil {
					t.Fatal("pending file not restored", err)
				}
			}
			if _, err = os.Stat(filepath.Join(root, ".delete-"+operation.ID)); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("private quarantine survived recovery", err)
			}
		})
	}
}
