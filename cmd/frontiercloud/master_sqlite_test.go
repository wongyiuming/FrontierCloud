package main

import (
	"bytes"
	"context"
	"path/filepath"
	"reflect"
	"testing"

	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	"github.com/wongyiuming/FrontierCloud/migrations"
)

func TestOfflineMySQLLockReadsLiveStatisticsBeforeFence(t *testing.T) {
	expected := []string{"SET SESSION time_zone='+00:00'", "SET SESSION information_schema_stats_expiry=0", "FLUSH TABLES WITH READ LOCK"}
	if got := migrationMySQLLockPlan(); !reflect.DeepEqual(got, expected) {
		t.Fatal("offline reader must disable cached metadata before its authoritative fence", got)
	}
}

func TestTransferAutoDDLPreservesAllCanonicalDefinitions(t *testing.T) {
	statements, err := migrations.Statements("sqlite")
	if err != nil {
		t.Fatal(err)
	}
	autos := map[string]string{"admin_audit_log": "id", "webrtc_observation_events": "id", "ip_security_audit_log": "id", "ip_auto_ban_events": "id", "karaoke_audit_log": "id", "node_identity": "singleton"}
	for _, statement := range statements {
		match := tablePattern.FindStringSubmatch(statement)
		if len(match) != 2 {
			continue
		}
		column, ok := autos[match[1]]
		if !ok {
			continue
		}
		ddl, err := transferAutoDDL(statement, column)
		if err != nil {
			t.Fatal(match[1], err)
		}
		db, err := sqlitestore.Open(filepath.Join(t.TempDir(), "test.db"))
		if err != nil {
			t.Fatal(err)
		}
		if _, err = db.Database().Exec(ddl); err != nil {
			t.Fatal(match[1], err)
		}
		db.Close()
		delete(autos, match[1])
	}
	if len(autos) != 0 {
		t.Fatal("missing auto tables", autos)
	}
}

func TestTransferDigestDistinguishesNullEmptyBinaryAndMicroseconds(t *testing.T) {
	db, err := sqlitestore.Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	db.Database().Exec("CREATE TABLE example(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT,payload BLOB,stamp DATETIME,active TEXT GENERATED ALWAYS AS (CASE WHEN value IS NOT NULL THEN value ELSE NULL END) STORED)")
	db.Database().Exec("INSERT INTO example(id,value,payload,stamp) VALUES(1,NULL,?,?)", []byte{0, 255}, "2026-10-05 00:00:00.123456")
	table := sqliteTransferTable{Name: "example", Columns: []string{"id", "value", "payload", "stamp", "active"}, NextID: 42}
	ctx := context.Background()
	count, digest, err := transferRowDigest(ctx, db.Database(), "sqlite", table)
	if err != nil || count != 1 {
		t.Fatal(count, err)
	}
	db.Database().Exec("UPDATE example SET value='' WHERE id=1")
	_, changed, err := transferRowDigest(ctx, db.Database(), "sqlite", table)
	if err != nil || changed == digest {
		t.Fatal("NULL conflated with empty/generated value", err)
	}
	db.Database().Exec("UPDATE example SET value=NULL,stamp='2026-10-05 00:00:00.123457'")
	_, changed, err = transferRowDigest(ctx, db.Database(), "sqlite", table)
	if err != nil || changed == digest {
		t.Fatal("timestamp precision lost", err)
	}
	db.Database().Exec("UPDATE sqlite_sequence SET seq=41 WHERE name='example'")
	tables, err := sqliteTransferInventory(ctx, db.Database(), []sqliteTransferTable{table})
	if err != nil || tables[0].NextID != 42 {
		t.Fatal(tables, err)
	}
	result, err := db.Database().Exec("INSERT INTO example(value) VALUES ('later')")
	if err != nil {
		t.Fatal(err)
	}
	id, _ := result.LastInsertId()
	if id != 42 {
		t.Fatal("deleted high ID reused", id)
	}
}

func TestMasterToSQLiteRejectsMissingProofBeforeOpeningAnyStore(t *testing.T) {
	var out bytes.Buffer
	if err := masterToSQLiteCommand(nil, &out); err == nil || out.Len() != 0 {
		t.Fatal("unproven migration accepted", err)
	}
	for _, parts := range [][2]string{{"/one", "/one/child"}, {"/one", "/one"}} {
		if !within(parts[0], parts[1]) {
			t.Fatal(parts)
		}
	}
	if within("/one", "/one_other") || within("/one", "/two") {
		t.Fatal("path boundary lost")
	}
}
