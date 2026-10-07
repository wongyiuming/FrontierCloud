package media

import (
	"context"
	"errors"
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

func masterFixture(t *testing.T) (string, *sqlitestore.Store, *Service) {
	t.Helper()
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	id, err := node.Initialize(ctx, db.Nodes(), filepath.Join(t.TempDir(), "secrets"))
	if err != nil {
		t.Fatal(err)
	}
	svc.identity = id
	err = svc.WithPromotion(ctx, "Master", func(p store.NodePromotion) error {
		p.Endpoint, p.Allocation = "https://master.test", 10*store.GiB
		_, err := db.Nodes().PromoteIdentity(ctx, p, store.NodeAudit{})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	svc.ConfigureCluster(db.Nodes(), db.Pool(), nil)
	return root, db, svc
}
func masterFunds(t *testing.T, db *sqlitestore.Store, deltaUsed, reserved int64) {
	t.Helper()
	var used, r int64
	if err := db.Database().QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_kind='MasterLocal'").Scan(&used, &r); err != nil || used != 30+deltaUsed || r != reserved {
		t.Fatalf("Master funds %d/%d want %d/%d: %v", used, r, 30+deltaUsed, reserved, err)
	}
}
func TestMasterLocalUploadVirtualDirectoriesValidationAndCancellation(t *testing.T) {
	root, db, svc := masterFixture(t)
	ctx := context.Background()
	v, err := svc.ReserveMasterUpload(ctx, "繁體.mp3", "music/NewVirtual", "", "primary", 10, 255, store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	masterFunds(t, db, 0, 10)
	for _, payload := range []string{"ID3short", "not-audio!", "ID3too-long"} {
		if _, err = svc.UploadMasterBytes(ctx, v.ID, strings.NewReader(payload), store.AdminAudit{}); err == nil {
			t.Fatal("invalid bytes accepted", payload)
		}
		masterFunds(t, db, 0, 10)
	}
	result, err := svc.UploadMasterBytes(ctx, v.ID, strings.NewReader("ID3payload"), store.AdminAudit{RequestID: "master-publish"})
	if err != nil || result.MediaID != v.MediaID {
		t.Fatal(result, err)
	}
	masterFunds(t, db, 10, 0)
	o, err := db.Media().ObjectByID(ctx, v.MediaID)
	if err != nil || o == nil || o.Path != v.Path {
		t.Fatal("stable local identity", o, err)
	}
	g, err := db.Pool().Resource(ctx, v.MediaID)
	if err != nil || g.ID != o.ID || g.Bytes != 10 {
		t.Fatal(g, err)
	}
	var wg sync.WaitGroup
	for range 16 {
		wg.Go(func() {
			if _, err := svc.UploadMasterBytes(ctx, v.ID, strings.NewReader("wrong"), store.AdminAudit{}); err != nil && !errors.Is(err, store.ErrNodeState) {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if err := svc.CancelMasterUpload(ctx, v.ID, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, v.Path)); err != nil {
		t.Fatal("cancel deleted completed file", err)
	}
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='upload-finalized' AND request_id='master-publish'").Scan(&count); err != nil || count != 1 {
		t.Fatal(count, err)
	}
	cancel, err := svc.ReserveMasterUpload(ctx, "cancel.mp3", "music/CancelVirtual", "", "primary", 10, 255, store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	if err := svc.CancelMasterUpload(ctx, cancel.ID, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	masterFunds(t, db, 10, 0)
	if _, err := db.Pool().Upload(ctx, cancel.ID); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("cancel retained reservation", err)
	}
}

type ambiguousMasterRepo struct {
	store.MediaRepository
	store.PoolRepository
}

func (r *ambiguousMasterRepo) CompleteMasterUpload(ctx context.Context, id string, o store.MediaObject, size int64, etag string, free int64, a store.AdminAudit) error {
	if err := r.PoolRepository.CompleteMasterUpload(ctx, id, o, size, etag, free, a); err != nil {
		return err
	}
	return errors.New("lost commit response")
}
func TestMasterPublicationCrashRecoveryKeepsIdentityQuotaAndExpiryProof(t *testing.T) {
	for _, scenario := range []string{"lost-commit", "expired-intent"} {
		t.Run(scenario, func(t *testing.T) {
			root, db, svc := masterFixture(t)
			ctx := context.Background()
			v, err := svc.ReserveMasterUpload(ctx, "song.mp3", "music/MasterRecovery", "", "primary", 10, 255, store.AdminAudit{})
			if err != nil {
				t.Fatal(err)
			}
			if scenario == "lost-commit" {
				fault, err := New(root, &ambiguousMasterRepo{db.Media(), db.Pool()}, svc.identity)
				if err != nil {
					t.Fatal(err)
				}
				fault.ConfigureCluster(db.Nodes(), fault.pool, nil)
				if _, err := fault.UploadMasterBytes(ctx, v.ID, strings.NewReader("ID3payload"), store.AdminAudit{RequestID: "master-recovery"}); !errors.Is(err, ErrRecovery) {
					t.Fatal(err)
				}
				fault.Close()
			} else {
				stage, err := svc.Stage(ctx, strings.NewReader("ID3payload"), 10)
				if err != nil {
					t.Fatal(err)
				}
				journal := uploadJournal{Format: "frontiercloud-master-upload", Version: 1, ID: stage.id, UploadSessionID: v.ID, Object: store.MediaObject{ID: v.MediaID, Path: v.Path, Kind: "audio"}, Bytes: 10, SHA256: stage.Digest, Audit: store.AdminAudit{RequestID: "master-recovery"}}
				if err := svc.writeUploadJournal(journal); err != nil {
					t.Fatal(err)
				}
				stage.retain = true
				stage.Close()
				if _, err := db.Database().Exec("UPDATE cluster_upload_sessions SET expires_at=? WHERE upload_id=?", time.Now().Unix()-1, v.ID); err != nil {
					t.Fatal(err)
				}
				if _, err := db.Pool().FinalizeUpload(ctx, v.ID, v.MediaID, 10, `"`+journal.SHA256+`"`, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
					t.Fatal("public finalize accepted expired intent", err)
				}
			}
			recovered, err := New(root, db.Media(), svc.identity)
			if err != nil {
				t.Fatal(err)
			}
			defer recovered.Close()
			masterFunds(t, db, 10, 0)
			stat, err := recovered.StorageStat(ctx, v.MediaID, v.Path)
			if err != nil || stat.Bytes != 10 {
				t.Fatal(stat, err)
			}
			var count int
			if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='upload-finalized' AND request_id='master-recovery'").Scan(&count); err != nil || count != 1 {
				t.Fatal("replay duplicated audit", count, err)
			}
		})
	}
}
func TestMasterCancelCannotRaceLiveStreamOrUnlinkUnknownFile(t *testing.T) {
	root, db, svc := masterFixture(t)
	ctx := context.Background()
	v, err := svc.ReserveMasterUpload(ctx, "song.mp3", "music/CancelRace", "", "primary", 10, 255, store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	release, err := svc.uploadSessionLease(v.ID)
	if err != nil {
		t.Fatal(err)
	}
	if err := svc.CancelMasterUpload(ctx, v.ID, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("live stream canceled", err)
	}
	release()
	if err := os.MkdirAll(filepath.Join(root, "music/CancelRace"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, v.Path), []byte("unknown"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := svc.CancelMasterUpload(ctx, v.ID, store.AdminAudit{}); !errors.Is(err, os.ErrExist) {
		t.Fatal("unknown file unlinked", err)
	}
	masterFunds(t, db, 0, 10)
}
