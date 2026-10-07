package business_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/backup"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestBusinessBackupExportRepeatableSnapshotDatesAndPrivateTableExclusion(t *testing.T) {
	db := database(t)
	raw := db.(interface{ Database() *sql.DB }).Database()
	ctx := context.Background()
	identity, err := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "private-key-must-not-export"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("UPDATE node_identity SET `role`='Master' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	name := "music/" + t.Name() + "/track.mp3"
	ids, err := db.Media().EnsureObjects(ctx, []store.MediaObject{{Path: name, Kind: "audio"}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		raw.Exec("DELETE FROM media_visibility WHERE relative_path=?", name)
		raw.Exec("DELETE FROM media_playback_stats WHERE media_id=?", ids[name])
		raw.Exec("DELETE FROM media_objects WHERE media_id=?", ids[name])
		raw.Exec("DELETE FROM admin_audit_log WHERE request_id=?", t.Name())
		raw.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", identity.Role, identity.Endpoint)
	})
	if err := db.Media().SetHidden(ctx, []string{name}, true, store.AdminAudit{RequestID: t.Name()}); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("INSERT INTO media_playback_stats(media_id,media_path,play_score,preference,created_at,updated_at) SELECT media_id,media_path,1,0,created_at,updated_at FROM media_objects WHERE media_id=?", ids[name]); err != nil {
		t.Fatal(err)
	}
	var changed, scoreSeen, dateSeen bool
	var artifact bytes.Buffer
	encoder := json.NewEncoder(&artifact)
	const backupGeneration int64 = 1790000000000000123
	if err := encoder.Encode(map[string]any{"kind": "header", "version": 2, "generation": backupGeneration}); err != nil {
		t.Fatal(err)
	}
	allowed := map[string]bool{}
	err = db.Backups().ExportBusinessSnapshot(ctx, func(table string, row map[string]any) error {
		if err := encoder.Encode(map[string]any{"kind": "row", "table": table, "value": row}); err != nil {
			return err
		}
		allowed[table] = true
		if strings.Contains(table, "identity") || strings.Contains(table, "relationship") || strings.Contains(table, "business_backup") || table == "media_delete_operations" || table == "cluster_upload_sessions" {
			t.Fatal("private/runtime table leaked", table)
		}
		if table == "media_visibility" && row["relative_path"] == name {
			date, ok := row["updated_at"].(map[string]string)
			if !ok || date["$datetime"] == "" {
				t.Fatal("database-dependent datetime", row)
			}
			dateSeen = true
			// Commit two related updates after the export snapshot exists. Later
			// tables must still see the same pre-update database generation.
			tx, err := raw.BeginTx(ctx, nil)
			if err != nil {
				return err
			}
			defer tx.Rollback()
			if _, err := tx.Exec("UPDATE media_visibility SET hidden=0 WHERE relative_path=?", name); err != nil {
				return err
			}
			if _, err := tx.Exec("UPDATE media_playback_stats SET play_score=2 WHERE media_id=?", ids[name]); err != nil {
				return err
			}
			changed = true
			return tx.Commit()
		}
		if table == "media_playback_stats" && row["media_id"] == ids[name] {
			raw, err := json.Marshal(row["play_score"])
			if err != nil || string(raw) != "1" {
				t.Fatal("mixed snapshot generations", row, err)
			}
			scoreSeen = true
		}
		return nil
	})
	if err != nil || !changed || !scoreSeen || !dateSeen || !allowed["ip_security_projection"] {
		t.Fatal("snapshot export", err, changed, scoreSeen, dateSeen, allowed)
	}
	if err := encoder.Encode(map[string]any{"kind": "end"}); err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(artifact.Bytes())
	report, err := backup.Preflight(ctx, bytes.NewReader(artifact.Bytes()), backup.Expectation{Generation: backupGeneration, Checksum: hex.EncodeToString(hash[:]), Bytes: int64(artifact.Len())}, t.TempDir())
	if err != nil || !report.LogicalValid || report.RestoreReady {
		t.Fatal("real driver exported artifact preflight", report, err)
	}
	var score int
	if err := raw.QueryRow("SELECT play_score FROM media_playback_stats WHERE media_id=?", ids[name]).Scan(&score); err != nil || score != 2 {
		t.Fatal("concurrent writer did not commit", score, err)
	}
	operation := strings.Repeat("d", 32)
	if _, err := raw.Exec("INSERT INTO media_delete_operations(operation_id,state,manifest,created_at) SELECT ?,'rename_pending','[]',created_at FROM media_objects WHERE media_id=?", operation, ids[name]); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { raw.Exec("DELETE FROM media_delete_operations WHERE operation_id=?", operation) })
	called := false
	if err := db.Backups().ExportBusinessSnapshot(ctx, func(string, map[string]any) error { called = true; return nil }); !errors.Is(err, store.ErrBackupBusy) || called {
		t.Fatal("omitted pending intent exported", err, called)
	}
	if _, err := raw.Exec("DELETE FROM media_delete_operations WHERE operation_id=?", operation); err != nil {
		t.Fatal(err)
	}
	sentinel := errors.New("output failure")
	if err := db.Backups().ExportBusinessSnapshot(ctx, func(string, map[string]any) error { return sentinel }); !errors.Is(err, sentinel) {
		t.Fatal("writer failure ignored", err)
	}
	if _, err := raw.Exec("UPDATE node_identity SET `role`='Follower' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	if err := db.Backups().ExportBusinessSnapshot(ctx, func(string, map[string]any) error { return nil }); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("Follower business snapshot exposed", err)
	}
}
