package business_test

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestGlobalRenameDurableScopeFencesConcurrentCommitAndMetadataIdentity(t *testing.T) {
	db := database(t)
	d := db.(interface{ Database() *sql.DB }).Database()
	ctx := context.Background()
	row, err := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "fixture", CreatedAt: time.Now().Unix()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Nodes().PromoteIdentity(ctx, store.NodePromotion{Role: "Master", Endpoint: "https://rename.test", Allocation: 5 * store.GiB, PhysicalFree: 10 * store.GiB}, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	old, target := "music/NativeGlobalRename%_!", "music/NativeGlobalRename%_!New"
	caseSibling := "music/nativeglobalrename%_!"
	uploads := []store.UploadReservation{}
	var opID string
	t.Cleanup(func() {
		for _, v := range uploads {
			for _, table := range []string{"global_media_objects", "cluster_upload_sessions", "media_objects", "media_playback_events", "media_playback_stats", "media_lyric_links"} {
				d.Exec("DELETE FROM "+table+" WHERE media_id=?", v.MediaID)
			}
		}
		d.Exec("DELETE FROM media_delete_operations WHERE operation_id=?", opID)
		d.Exec("DELETE FROM media_visibility WHERE relative_path IN (?,?,?)", old, target, caseSibling)
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			d.Exec("DELETE FROM "+table+" WHERE member_id=?", row.ID)
		}
		d.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", row.Role, row.Endpoint)
		d.Exec("DELETE FROM admin_audit_log WHERE request_id='native-global-rename'")
	})
	for _, name := range []string{old + "/one.mp3", old + "/two.mp3", caseSibling + "/one.mp3"} {
		v, err := db.Pool().ReserveUpload(ctx, name, "primary", 10, 10*store.GiB, store.AdminAudit{})
		if err != nil {
			t.Fatal(err)
		}
		uploads = append(uploads, v)
		if err := db.Pool().CompleteMasterUpload(ctx, v.ID, store.MediaObject{ID: v.MediaID, Path: v.Path, Kind: v.Kind}, 10, `"`+strings.Repeat("f", 64)+`"`, 10*store.GiB, store.AdminAudit{}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.Pool().RecordGlobalPlayback(ctx, uploads[0].MediaID, "20512c3b-5340-4185-b76d-20402279482a"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Pool().SetGlobalPreference(ctx, uploads[0].MediaID, 42, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := db.Media().SetHidden(ctx, []string{old, caseSibling}, true, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := db.Media().BindLyric(ctx, store.MediaObject{ID: uploads[0].MediaID, Path: uploads[0].Path, Kind: "audio"}, store.MediaObject{Path: "lyrics/NativeGlobalRename.lrc", Kind: "lyric"}); err != nil {
		t.Fatal(err)
	}
	reservation, err := db.Pool().ReserveUpload(ctx, target+"/pending.mp3", "primary", 10, 10*store.GiB, store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Pool().PrepareGlobalRename(ctx, old, target, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("live target reservation ignored", err)
	}
	if err := db.Pool().ReleaseCleanedUpload(ctx, reservation.ID, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	ids := make(chan string, 16)
	for range 16 {
		wg.Go(func() {
			op, err := db.Pool().PrepareGlobalRename(ctx, old, target, store.AdminAudit{RequestID: "native-global-rename"})
			if err != nil {
				t.Error(err)
				return
			}
			if len(op.Media) != 2 || op.State != "rename_pending" {
				t.Error("scope/case selection", op)
			}
			ids <- op.ID
		})
	}
	wg.Wait()
	close(ids)
	for id := range ids {
		if opID == "" {
			opID = id
		}
		if id != opID {
			t.Fatal("concurrent operation duplicated", id, opID)
		}
	}
	if opID == "" {
		t.Fatal("no prepared operation")
	}
	if rows, err := db.Pool().Resources(ctx, old, false); err != nil || len(rows) != 0 {
		t.Fatal("partial rename visible", rows, err)
	}
	for _, name := range []string{old + "/fresh.mp3", target + "/fresh.mp3"} {
		if _, err := db.Pool().ReserveUpload(ctx, name, "primary", 10, 10*store.GiB, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
			t.Fatal("rename scope reused", name, err)
		}
		if _, err := db.Pool().PrepareGlobalDelete(ctx, []store.DeleteItem{{Path: name}}, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
			t.Fatal("rename scope deleted", name, err)
		}
	}
	if _, err := db.Pool().PrepareGlobalDelete(ctx, []store.DeleteItem{{Path: "music", Directory: true}}, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("ancestor deletion raced rename", err)
	}
	if operations, err := db.Media().DeleteOperations(ctx); err != nil || len(operations) != 0 {
		t.Fatal("rename decoded as deletion", operations, err)
	}
	if err := db.Media().ForgetDelete(ctx, opID); err != nil {
		t.Fatal(err)
	}
	if op, err := db.Pool().GlobalRename(ctx, opID); err != nil || op == nil {
		t.Fatal("delete cleaner erased rename intent", op, err)
	}
	var used, reserved int64
	if err := d.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", row.ID).Scan(&used, &reserved); err != nil || used != 30 || reserved != 0 {
		t.Fatal("prepare changed funds", used, reserved, err)
	}
	for range 16 {
		wg.Go(func() {
			if err := db.Pool().CompleteGlobalRename(ctx, opID); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if op, err := db.Pool().GlobalRename(ctx, opID); err != nil || op == nil || op.State != "rename_cleanup" {
		t.Fatal("cleanup phase not durable", op, err)
	}
	if _, err := db.Pool().ReserveUpload(ctx, target+"/until-cleanup.mp3", "primary", 10, 10*store.GiB, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("namespace released before cleanup", err)
	}
	if err := db.Pool().CompleteGlobalRenameCleanup(ctx, opID); err != nil {
		t.Fatal(err)
	}
	for i, v := range uploads {
		actual, err := db.Pool().Resource(ctx, v.MediaID)
		if err != nil {
			t.Fatal(err)
		}
		want := v.Path
		if i < 2 {
			want = target + strings.TrimPrefix(v.Path, old)
		}
		if actual.Path != want || actual.ObjectID != v.MediaID || actual.Bytes != 10 || actual.State != "active" {
			t.Fatal("global identity changed", actual)
		}
		local, err := db.Media().ObjectByID(ctx, v.MediaID)
		if err != nil || local == nil || local.Path != want {
			t.Fatal("local path not atomic", local, err)
		}
		ledger, err := db.Pool().Upload(ctx, v.ID)
		if err != nil || ledger.Path != want || ledger.State != "complete" {
			t.Fatal("completed session stale", ledger, err)
		}
	}
	stats, err := db.Media().Stats(ctx, []string{uploads[0].MediaID})
	if err != nil || stats[uploads[0].MediaID].PlayScore != 1 || stats[uploads[0].MediaID].Preference != 42 {
		t.Fatal(stats, err)
	}
	lyric, err := db.Media().LyricPath(ctx, uploads[0].MediaID)
	if err != nil || lyric != "lyrics/NativeGlobalRename.lrc" {
		t.Fatal(lyric, err)
	}
	hidden, err := db.Media().HiddenPaths(ctx)
	if err != nil || hidden[old] || !hidden[target] || !hidden[caseSibling] {
		t.Fatal(hidden, err)
	}
	var count int
	if err := d.QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='directory_rename' AND request_id='native-global-rename'").Scan(&count); err != nil || count != 1 {
		t.Fatal("duplicate success audit", count, err)
	}
	if err := d.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", row.ID).Scan(&used, &reserved); err != nil || used != 30 || reserved != 0 {
		t.Fatal("rename changed funds", used, reserved, err)
	}
}
