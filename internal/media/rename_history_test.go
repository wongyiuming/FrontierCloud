package media

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestRenameHistoryDrainRefusesPhysicalIntentsAndPreservesLiveBytes(t *testing.T) {
	root, db, svc := masterFixture(t)
	ctx := context.Background()
	n, err := db.Nodes().ReadIdentity(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = svc.Rename(ctx, "music/artist", "HistoryArtist", store.AdminAudit{RequestID: "history-fixture"}); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(root, "music", "HistoryArtist", ".global-rename-unknown.marker")
	if err = os.WriteFile(marker, []byte("unproven"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = DrainRenameHistory(ctx, root, db.Maintenance(), db.Media(), n.ID); !errors.Is(err, ErrRecovery) {
		t.Fatal("physical intent ignored", err)
	}
	var count int
	if err = db.Database().QueryRow("SELECT COUNT(*) FROM media_delete_operations WHERE state='rename_done'").Scan(&count); err != nil || count != 1 {
		t.Fatal("refusal erased history", count, err)
	}
	if err = os.Remove(marker); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		want := 1
		if i == 1 {
			want = 0
		}
		count, err := DrainRenameHistory(ctx, root, db.Maintenance(), db.Media(), n.ID)
		if err != nil || count != want {
			t.Fatal(count, err)
		}
	}
	if data, err := os.ReadFile(filepath.Join(root, "music", "HistoryArtist", "song.mp3")); err != nil || string(data) != "ID3payload" {
		t.Fatal("drain touched media", err)
	}
	if err = db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='directory_rename' AND request_id='history-fixture'").Scan(&count); err != nil || count != 1 {
		t.Fatal("success audit removed", count, err)
	}
}
