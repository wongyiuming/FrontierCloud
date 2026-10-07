package media

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func ownedFixture(t *testing.T) (string, *sqlitestore.Store, *Service, string) {
	t.Helper()
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	identity, err := node.Initialize(ctx, db.Nodes(), filepath.Join(t.TempDir(), "secrets"))
	if err != nil {
		t.Fatal(err)
	}
	svc.identity = identity
	if _, err = db.Nodes().PromoteIdentity(ctx, store.NodePromotion{Role: "Follower", Endpoint: "https://follower.test"}, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	now := time.Now().Unix()
	p := store.PairPackage{Nonce: strings.Repeat("b", 32), TokenHash: strings.Repeat("c", 64), ExpiresAt: now + 300}
	if _, err = db.Nodes().IssuePair(ctx, p, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	rel := store.Relationship{ID: strings.Repeat("d", 32), PeerID: strings.Repeat("e", 32), Endpoint: "https://master.test", PublicKey: strings.Repeat("A", 43), Credential: "sealed-fixture", Direction: "upstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: now}
	if err = db.Nodes().ConsumePair(ctx, p, rel, now, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err = db.Nodes().ActivateRelationship(ctx, rel.ID, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled = true
	cfg.Storage.Allocation = 2 * store.GiB
	if err = db.Pool().AcceptFollowerConfiguration(ctx, rel.ID, cfg, 10*store.GiB); err != nil {
		t.Fatal(err)
	}
	return root, db, svc, rel.ID
}
func ownedFunds(t *testing.T, db *sqlitestore.Store, used, reserved int64) {
	t.Helper()
	var u, r int64
	if err := db.Database().QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_kind='Follower'").Scan(&u, &r); err != nil || u != used || r != reserved {
		t.Fatalf("funds used=%d reserved=%d want=%d/%d error=%v", u, r, used, reserved, err)
	}
}
func TestOwnedUploadBoundedValidatedIdempotentAndFailedStreamCleanup(t *testing.T) {
	_, db, svc, relationship := ownedFixture(t)
	ctx := context.Background()
	object := store.MediaObject{ID: strings.Repeat("1", 64), Path: "music/OwnedStorage/song.mp3", Kind: "audio"}
	for _, payload := range []string{"ID3short", "not-audio!", "ID3too-long"} {
		if _, err := svc.OwnedUpload(ctx, relationship, object, 10, strings.NewReader(payload), store.NodeAudit{}); err == nil {
			t.Fatal("invalid payload accepted", payload)
		}
		ownedFunds(t, db, 0, 0)
	}
	var group sync.WaitGroup
	for range 16 {
		group.Go(func() {
			receipt, err := svc.OwnedUpload(ctx, relationship, object, 10, strings.NewReader("ID3payload"), store.NodeAudit{})
			if err != nil && !errors.Is(err, store.ErrNodeState) && !errors.Is(err, os.ErrExist) {
				t.Error(err)
			}
			if err == nil && (receipt.Bytes != 10 || receipt.ObjectID != object.ID) {
				t.Error(receipt)
			}
		})
	}
	group.Wait()
	ownedFunds(t, db, 10, 0)
	receipt, err := svc.StorageStat(ctx, object.ID, object.Path)
	if err != nil || receipt.Bytes != 10 || len(receipt.SHA256) != 64 {
		t.Fatal(receipt, err)
	}
	if _, err = svc.OwnedUpload(ctx, relationship, object, 10, strings.NewReader("different!"), store.NodeAudit{}); err != nil {
		t.Fatal("publication lost-response retry", err)
	}
	var count int
	if err = db.Database().QueryRow("SELECT COUNT(*) FROM node_audit WHERE action='storage-upload-published'").Scan(&count); err != nil || count != 1 {
		t.Fatal("publication duplicate audit", count, err)
	}
	large := store.MediaObject{ID: strings.Repeat("2", 64), Path: "music/OwnedStorage/large.mp3", Kind: "audio"}
	reader := &boundedUploadReader{left: 8 * 1024 * 1024}
	if _, err = svc.OwnedUpload(ctx, relationship, large, 8*1024*1024, reader, store.NodeAudit{}); err != nil || reader.maximum > 64*1024 {
		t.Fatal("unbounded upload", reader.maximum, err)
	}
	ownedFunds(t, db, 10+8*1024*1024, 0)
	if _, err = svc.StorageStat(ctx, object.ID, "music/other/song.mp3"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("stat accepted different path")
	}
}

type ambiguousOwnedRepo struct {
	store.MediaRepository
	store.OwnedStorageRepository
}

func (r *ambiguousOwnedRepo) CompleteOwnedUpload(ctx context.Context, id string, o store.MediaObject, size int64, etag string, free int64, a store.NodeAudit) error {
	if err := r.OwnedStorageRepository.CompleteOwnedUpload(ctx, id, o, size, etag, free, a); err != nil {
		return err
	}
	return errors.New("lost commit acknowledgement")
}
func TestOwnedUploadLostCommitAndInterruptedIntentRecoverExactlyOnce(t *testing.T) {
	for _, phase := range []string{"lost_commit", "before_publication", "abandoned_stream"} {
		t.Run(phase, func(t *testing.T) {
			root, db, svc, rel := ownedFixture(t)
			ctx := context.Background()
			identity := svc.identity
			object := store.MediaObject{ID: strings.Repeat("3", 64), Path: "music/Recovery/song.mp3", Kind: "audio"}
			if phase == "lost_commit" {
				svc.Close()
				fault, err := New(root, &ambiguousOwnedRepo{db.Media(), db.Pool()}, identity)
				if err != nil {
					t.Fatal(err)
				}
				if _, err = fault.OwnedUpload(ctx, rel, object, 10, strings.NewReader("ID3payload"), store.NodeAudit{RequestID: "owned-recovery"}); !errors.Is(err, ErrRecovery) {
					t.Fatal("ambiguous commit not fenced", err)
				}
				fault.Close()
				ownedFunds(t, db, 10, 0)
			} else {
				stage, err := svc.Stage(ctx, strings.NewReader("ID3payload"), 10)
				if err != nil {
					t.Fatal(err)
				}
				stage.retain = true
				if err = db.Pool().ReserveOwnedUpload(ctx, stage.id, rel, object, 10, 10*store.GiB, store.NodeAudit{}); err != nil {
					t.Fatal(err)
				}
				if phase == "before_publication" {
					if err = svc.writeUploadJournal(uploadJournal{Format: "frontiercloud-owned-upload", Version: 1, ID: stage.id, Object: object, Bytes: 10, SHA256: stage.Digest}); err != nil {
						t.Fatal(err)
					}
				}
				stage.Close()
				svc.Close()
				ownedFunds(t, db, 0, 10)
			}
			recovered, err := New(root, db.Media(), identity)
			if err != nil {
				t.Fatal(err)
			}
			defer recovered.Close()
			if phase == "abandoned_stream" {
				ownedFunds(t, db, 0, 0)
				if _, err = os.Stat(filepath.Join(root, object.Path)); !errors.Is(err, os.ErrNotExist) {
					t.Fatal("abandoned bytes were published")
				}
				return
			}
			ownedFunds(t, db, 10, 0)
			var count int
			if err = db.Database().QueryRow("SELECT COUNT(*) FROM node_audit WHERE action='storage-upload-published'").Scan(&count); err != nil || count != 1 {
				t.Fatal("replay duplicated accounting", count, err)
			}
			stat, err := recovered.StorageStat(ctx, object.ID, object.Path)
			if err != nil || stat.Bytes != 10 {
				t.Fatal(stat, err)
			}
		})
	}
}

type pausedOwnedReader struct {
	started, proceed chan struct{}
	once             sync.Once
	reader           io.Reader
}

func (r *pausedOwnedReader) Read(p []byte) (int, error) {
	r.once.Do(func() { close(r.started); <-r.proceed })
	return r.reader.Read(p)
}
func TestOwnedRecoveryPreservesLiveWorkerReservation(t *testing.T) {
	root, db, svc, rel := ownedFixture(t)
	ctx := context.Background()
	object := store.MediaObject{ID: strings.Repeat("4", 64), Path: "music/Live/song.mp3", Kind: "audio"}
	reader := &pausedOwnedReader{started: make(chan struct{}), proceed: make(chan struct{}), reader: strings.NewReader("ID3payload")}
	done := make(chan error, 1)
	go func() { _, err := svc.OwnedUpload(ctx, rel, object, 10, reader, store.NodeAudit{}); done <- err }()
	select {
	case <-reader.started:
	case <-time.After(10 * time.Second):
		t.Fatal("upload did not reserve before read")
	}
	ownedFunds(t, db, 0, 10)
	recovered, err := New(root, db.Media(), svc.identity)
	if err != nil {
		close(reader.proceed)
		<-done
		t.Fatal(err)
	}
	defer recovered.Close()
	ownedFunds(t, db, 0, 10)
	close(reader.proceed)
	if err = <-done; err != nil {
		t.Fatal(err)
	}
	ownedFunds(t, db, 10, 0)
}
