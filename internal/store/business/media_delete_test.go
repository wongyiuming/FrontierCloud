package business_test

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"fmt"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestMediaDeleteJournalAndExactMetadataScope(t *testing.T) {
	db := database(t)
	repo := db.Media()
	ctx := context.Background()
	base := "music/" + t.Name() + "%_!"
	objects := []store.MediaObject{{Path: base + "/song.mp3", Kind: "audio"}, {Path: strings.ToUpper(base) + "/song.mp3", Kind: "audio"}}
	ids, err := repo.EnsureObjects(ctx, objects)
	if err != nil {
		t.Fatal(err)
	}
	for _, object := range objects {
		if _, err := repo.RecordPlayback(ctx, object, "8b4d009a-4be7-4d10-a3a4-e3d7e80394a5"); err != nil {
			t.Fatal(err)
		}
		if err := repo.BindLyric(ctx, object, store.MediaObject{Path: "lyrics/" + t.Name() + ".lrc", Kind: "lyric"}); err != nil {
			t.Fatal(err)
		}
	}
	if err := repo.SetHidden(ctx, []string{base, base + "/child", strings.ToUpper(base) + "/child"}, true, store.AdminAudit{Action: "hide"}); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte(t.Name()))
	operation := store.DeleteOperation{ID: fmt.Sprintf("%x", sum[:16]), State: "pending", Items: []store.DeleteItem{{Path: base, Slot: "0", Directory: true}}}
	if err := repo.PrepareDelete(ctx, operation, store.AdminAudit{Action: "delete", RequestID: "delete-test"}); err != nil {
		t.Fatal(err)
	}
	prepared, err := repo.DeleteOperation(ctx, operation.ID)
	if err != nil || prepared == nil || prepared.State != "pending" || len(prepared.Items) != 1 {
		t.Fatalf("pending journal: %+v %v", prepared, err)
	}
	if err := repo.CommitDelete(ctx, operation.ID, store.AdminAudit{Action: "delete", RequestID: "delete-test"}); err != nil {
		t.Fatal(err)
	}
	if err := repo.CommitDelete(ctx, operation.ID, store.AdminAudit{Action: "delete", RequestID: "delete-test"}); err != nil {
		t.Fatal("commit not idempotent", err)
	}
	for i, object := range objects {
		found, err := repo.ObjectByID(ctx, ids[object.Path])
		if err != nil || (i == 0 && found != nil) || (i == 1 && found == nil) {
			t.Fatalf("object scope %s: %+v %v", object.Path, found, err)
		}
	}
	hidden, err := repo.HiddenPaths(ctx)
	if err != nil || hidden[base] || hidden[base+"/child"] || !hidden[strings.ToUpper(base)+"/child"] {
		t.Fatalf("visibility scope: %v %v", hidden, err)
	}
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	for _, table := range []string{"media_playback_events", "media_playback_stats", "media_lyric_links"} {
		for i, object := range objects {
			var count int
			if err := sqlDB.QueryRow("SELECT COUNT(*) FROM "+table+" WHERE media_id=?", ids[object.Path]).Scan(&count); err != nil || i == 0 && count != 0 || i == 1 && count != 1 {
				t.Fatalf("metadata scope %s %d: %d %v", table, i, count, err)
			}
		}
	}
	var auditCount int
	if err := sqlDB.QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE request_id=?", "delete-test").Scan(&auditCount); err != nil || auditCount != 2 {
		t.Fatalf("delete audits: %d %v", auditCount, err)
	}
	committed, err := repo.DeleteOperation(ctx, operation.ID)
	if err != nil || committed == nil || committed.State != "committed" {
		t.Fatalf("committed journal: %+v %v", committed, err)
	}
	if err := repo.ForgetDelete(ctx, operation.ID); err != nil {
		t.Fatal(err)
	}
	if forgotten, err := repo.DeleteOperation(ctx, operation.ID); err != nil || forgotten != nil {
		t.Fatalf("forgotten journal: %+v %v", forgotten, err)
	}
}
