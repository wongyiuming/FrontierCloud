package business_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestMediaAuditRetainsLargeBatchEvidence(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	paths := []string{}
	for i := range 80 {
		paths = append(paths, fmt.Sprintf("music/%s/%s%03d", t.Name(), strings.Repeat("a", 170), i))
	}
	if err := db.Media().SetHidden(ctx, paths, true, store.AdminAudit{Action: "hide", RequestID: "batch-evidence"}); err != nil {
		t.Fatal(err)
	}
	rows, err := db.(interface{ Database() *sql.DB }).Database().Query("SELECT source_summary,target_count,detail FROM admin_audit_log WHERE request_id=?", "batch-evidence")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	seen := map[string]bool{}
	batches := 0
	for rows.Next() {
		var source, detail string
		var count int
		if err := rows.Scan(&source, &count, &detail); err != nil {
			t.Fatal(err)
		}
		var targets []string
		if err := json.Unmarshal([]byte(source), &targets); err != nil || len(targets) != count {
			t.Fatalf("truncated evidence: %v %d", err, count)
		}
		var details map[string]any
		if err := json.Unmarshal([]byte(detail), &details); err != nil || details["affected_total"] != float64(len(paths)) {
			t.Fatalf("missing batch total: %s %v", detail, err)
		}
		for _, target := range targets {
			seen[target] = true
		}
		batches++
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if batches < 2 || len(seen) != len(paths) {
		t.Fatalf("batch evidence missing: %d batches, %d targets", batches, len(seen))
	}
}

func TestMediaMutationsAtomicAndCaseSensitive(t *testing.T) {
	db := database(t)
	repo := db.Media()
	ctx := context.Background()
	base := "music/" + t.Name() + "%_!"
	audit := store.AdminAudit{Action: "media_priority", SessionHash: strings.Repeat("a", 64), RequestID: "request-1"}
	object := store.MediaObject{Path: base + "/song.mp3", Kind: "audio"}
	play, err := repo.RecordPlayback(ctx, object, "93790cc1-01ee-44b7-8f0b-d142996b0305")
	if err != nil {
		t.Fatal(err)
	}
	value, err := repo.SetPreference(ctx, object, 500, audit)
	if err != nil || value.MediaID != play.MediaID || value.PlayScore != play.PlayScore || value.Preference != 500 {
		t.Fatalf("preference: %+v %v", value, err)
	}
	if _, err := repo.SetPreference(ctx, object, -8, audit); err == nil {
		t.Fatal("invalid preference accepted")
	}
	directory := store.MediaObject{Path: base, Kind: "directory"}
	if _, err := repo.SetPreference(ctx, directory, -7, audit); err != nil {
		t.Fatal(err)
	}
	priorities, err := repo.DirectoryPreferences(ctx)
	if err != nil || priorities[base] != -7 {
		t.Fatalf("directory priority: %v %v", priorities, err)
	}
	paths := []string{base, base + "/child", strings.ToUpper(base) + "/child", strings.ReplaceAll(base, "%_!", "other") + "/child"}
	if err := repo.SetHidden(ctx, paths, true, store.AdminAudit{Action: "hide"}); err != nil {
		t.Fatal(err)
	}
	if err := repo.SetHidden(ctx, []string{base}, false, store.AdminAudit{Action: "unhide"}); err != nil {
		t.Fatal(err)
	}
	hidden, err := repo.HiddenPaths(ctx)
	if err != nil || hidden[base] || hidden[base+"/child"] || !hidden[paths[2]] || !hidden[paths[3]] {
		t.Fatalf("unhide escaped prefix: %v %v", hidden, err)
	}
	var count int
	if err := db.(interface{ Database() *sql.DB }).Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE request_id=?", "request-1").Scan(&count); err != nil || count != 2 {
		t.Fatalf("atomic audits: %d %v", count, err)
	}
}

func TestSQLiteAuditFailureRollsBackMutation(t *testing.T) {
	if testing.Short() {
		t.Skip("transaction failure injection")
	}
	db := database(t)
	if db.Backend() != "sqlite" {
		t.Skip("SQLite trigger failure injection")
	}
	ctx := context.Background()
	repo := db.Media()
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	if _, err := sqlDB.Exec("CREATE TRIGGER reject_media_audit BEFORE INSERT ON admin_audit_log BEGIN SELECT RAISE(ABORT,'audit unavailable'); END"); err != nil {
		t.Fatal(err)
	}
	defer sqlDB.Exec("DROP TRIGGER reject_media_audit")
	object := store.MediaObject{Path: "music/failure/song.mp3", Kind: "audio"}
	if _, err := repo.SetPreference(ctx, object, 5, store.AdminAudit{Action: "media_priority"}); err == nil {
		t.Fatal("audit failure ignored")
	}
	var count int
	if err := sqlDB.QueryRow("SELECT COUNT(*) FROM media_objects WHERE media_path=?", object.Path).Scan(&count); err != nil || count != 0 {
		t.Fatalf("identity survived rollback: %d %v", count, err)
	}
	if err := repo.SetHidden(ctx, []string{"music/failure"}, true, store.AdminAudit{Action: "hide"}); err == nil {
		t.Fatal("audit failure ignored on hide")
	}
	hidden, err := repo.HiddenPaths(ctx)
	if err != nil || hidden["music/failure"] {
		t.Fatalf("hide survived rollback: %v %v", hidden, err)
	}
}
