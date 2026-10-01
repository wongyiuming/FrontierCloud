package media

import (
	"context"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestMasterLocalGlobalDeleteQuotaRollbackLostCommitAndCrashRecovery(t *testing.T) {
	for _, scenario := range []string{"normal", "missing-bytes", "audit-rollback", "pending-crash", "committed-crash"} {
		t.Run(scenario, func(t *testing.T) {
			root, db, svc := masterFixture(t)
			ctx := context.Background()
			v, err := svc.ReserveMasterUpload(ctx, "song.mp3", "music/GlobalDelete", "", "primary", 10, 255, store.AdminAudit{})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := svc.UploadMasterBytes(ctx, v.ID, strings.NewReader("ID3payload"), store.AdminAudit{}); err != nil {
				t.Fatal(err)
			}
			if _, err := db.Pool().RecordGlobalPlayback(ctx, v.MediaID, "20512c3b-5340-4185-b76d-20402279482a"); err != nil {
				t.Fatal(err)
			}
			if scenario == "audit-rollback" {
				if _, err := db.Database().Exec("CREATE TRIGGER fail_global_delete BEFORE INSERT ON admin_audit_log WHEN NEW.action='global-media-delete-completed' BEGIN SELECT RAISE(FAIL,'injected'); END"); err != nil {
					t.Fatal(err)
				}
				result, err := svc.DeleteGlobal(ctx, []string{"music/GlobalDelete"}, store.AdminAudit{})
				if err != nil || len(result.Pending) != 1 || result.Deleted != 0 {
					t.Fatal(result, err)
				}
				if _, err := os.Stat(filepath.Join(root, v.Path)); err != nil {
					t.Fatal("rollback did not restore physical bytes", err)
				}
				masterFunds(t, db, 10, 0)
				if _, err := db.Database().Exec("DROP TRIGGER fail_global_delete"); err != nil {
					t.Fatal(err)
				}
				if err := svc.RetryGlobalDeletes(ctx); err != nil {
					t.Fatal(err)
				}
			} else if strings.HasSuffix(scenario, "crash") {
				rows, err := db.Pool().PrepareGlobalDelete(ctx, []store.DeleteItem{{Path: v.Path}}, store.AdminAudit{})
				if err != nil || len(rows) != 1 {
					t.Fatal(rows, err)
				}
				op := store.DeleteOperation{ID: strings.Repeat("f", 32), State: "pending", Items: []store.DeleteItem{{Path: v.Path, Slot: "0", OwnedID: v.MediaID, GlobalID: v.MediaID, Bytes: 10}}}
				if err := db.Pool().PrepareMasterDelete(ctx, op, store.AdminAudit{}); err != nil {
					t.Fatal(err)
				}
				if err := svc.root.Mkdir(".delete-"+op.ID, 0700); err != nil {
					t.Fatal(err)
				}
				if err := svc.root.Rename(v.Path, ".delete-"+op.ID+"/0"); err != nil {
					t.Fatal(err)
				}
				if scenario == "committed-crash" {
					if err := db.Pool().CommitMasterDelete(ctx, op.ID, store.AdminAudit{}); err != nil {
						t.Fatal(err)
					}
				}
				recovered, err := New(root, db.Media(), svc.identity)
				if err != nil {
					t.Fatal(err)
				}
				defer recovered.Close()
				recovered.ConfigureCluster(db.Nodes(), db.Pool(), nil)
				if scenario == "pending-crash" {
					if _, err := os.Stat(filepath.Join(root, v.Path)); err != nil {
						t.Fatal("pending intent did not restore file", err)
					}
					masterFunds(t, db, 10, 0)
					if err := recovered.RetryGlobalDeletes(ctx); err != nil {
						t.Fatal(err)
					}
				}
			} else {
				if scenario == "missing-bytes" {
					if err := os.Remove(filepath.Join(root, v.Path)); err != nil {
						t.Fatal(err)
					}
				}
				if err := db.Pool().CompleteGlobalDelete(ctx, v.MediaID, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
					t.Fatal("local deletion bypassed quarantine", err)
				}
				result, err := svc.DeleteGlobal(ctx, []string{v.Path, "music/GlobalDelete", v.Path}, store.AdminAudit{})
				if err != nil || result.Deleted != 1 || len(result.Pending) != 0 {
					t.Fatal(result, err)
				}
			}
			masterFunds(t, db, 0, 0)
			if _, err := os.Stat(filepath.Join(root, v.Path)); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("deleted file remains/resurrected", err)
			}
			placement, err := db.Pool().GlobalPlacement(ctx, v.MediaID)
			if err != nil || placement != nil {
				t.Fatal("global metadata retained", placement, err)
			}
			for _, table := range []string{"media_objects", "media_playback_stats", "media_playback_events", "media_lyric_links"} {
				var count int
				if err := db.Database().QueryRow("SELECT COUNT(*) FROM "+table+" WHERE media_id=?", v.MediaID).Scan(&count); err != nil || count != 0 {
					t.Fatal(table, count, err)
				}
			}
			var count int
			if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='global-media-delete-completed'").Scan(&count); err != nil || count != 1 {
				t.Fatal("duplicate completion audit", count, err)
			}
			if _, err := svc.ReserveMasterUpload(ctx, "song.mp3", "music/GlobalDelete", "", "primary", 10, 255, store.AdminAudit{}); err != nil {
				t.Fatal("deleted path cannot be reused", err)
			}
		})
	}
}
