package business_test

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestRenameMetadataPreservesIdentityAndReusesOnlyStaleDirectories(t *testing.T) {
	db := database(t)
	repo := db.Media()
	ctx := context.Background()
	base := "music/" + t.Name() + "%_!"
	target := base + "-new"
	objects := []store.MediaObject{{Path: base + "/song.mp3", Kind: "audio"}, {Path: strings.ToUpper(base) + "/song.mp3", Kind: "audio"}, {Path: base, Kind: "directory"}, {Path: target, Kind: "directory"}}
	ids, err := repo.EnsureObjects(ctx, objects)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := repo.RecordPlayback(ctx, objects[0], "20512c3b-5340-4185-b76d-20402279482a"); err != nil {
		t.Fatal(err)
	}
	if err := repo.BindLyric(ctx, objects[0], store.MediaObject{Path: "lyrics/" + t.Name() + ".lrc", Kind: "lyric"}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.SetPreference(ctx, objects[2], 7, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.SetPreference(ctx, objects[3], -7, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := repo.SetHidden(ctx, []string{base, base + "/child", strings.ToUpper(base)}, true, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := repo.CompleteRename(ctx, base, target, strings.Repeat("e", 32), store.AdminAudit{RequestID: "rename-metadata-test"}); err != nil {
			t.Fatal(err)
		}
	}
	for i, object := range objects {
		actual, err := repo.ObjectByID(ctx, ids[object.Path])
		if err != nil {
			t.Fatal(err)
		}
		if i == 3 {
			if actual != nil {
				t.Fatal("stale directory identity retained")
			}
			continue
		}
		expected := object.Path
		if i != 1 {
			expected = target + strings.TrimPrefix(object.Path, base)
		}
		if actual == nil || actual.Path != expected {
			t.Fatalf("identity %d: %+v", i, actual)
		}
	}
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	for _, table := range []string{"media_playback_stats", "media_lyric_links"} {
		var name string
		if err := sqlDB.QueryRow("SELECT media_path FROM "+table+" WHERE media_id=?", ids[objects[0].Path]).Scan(&name); err != nil || name != target+"/song.mp3" {
			t.Fatalf("rewrite %s: %s %v", table, name, err)
		}
	}
	stats, err := repo.Stats(ctx, []string{ids[objects[0].Path], ids[base]})
	if err != nil || stats[ids[objects[0].Path]].PlayScore != 1 || stats[ids[base]].Preference != 7 {
		t.Fatalf("stats: %v %v", stats, err)
	}
	hidden, err := repo.HiddenPaths(ctx)
	if err != nil || hidden[base] || !hidden[target] || !hidden[target+"/child"] || !hidden[strings.ToUpper(base)] {
		t.Fatalf("case-sensitive visibility: %v %v", hidden, err)
	}
	var count int
	if err := sqlDB.QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE request_id='rename-metadata-test'").Scan(&count); err != nil || count != 1 {
		t.Fatal(fmt.Sprintf("rename audit %d %v", count, err))
	}
}
