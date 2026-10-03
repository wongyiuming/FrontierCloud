package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func TestVerifyBackupCommandOnlyEmitsLogicalReportForVerifiedInput(t *testing.T) {
	directory := t.TempDir()
	file, scratch := filepath.Join(directory, "backup.jsonl"), filepath.Join(directory, "scratch")
	data := []byte("{\"kind\":\"header\",\"version\":2,\"generation\":1790000000000000123}\n{\"kind\":\"end\"}\n")
	if err := os.WriteFile(file, data, 0600); err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(data)
	args := []string{"--file", file, "--generation", "1790000000000000123", "--sha256", hex.EncodeToString(hash[:]), "--scratch-dir", scratch}
	var output bytes.Buffer
	if err := verifyBackupCommand(args, &output); err != nil {
		t.Fatal(err)
	}
	var report struct {
		LogicalValid, RestoreReady bool
		PendingGates               []string
	}
	// Decode with actual wire names, not Go's CamelCase matching.
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(output.Bytes(), &fields); err != nil {
		t.Fatal(err)
	}
	json.Unmarshal(fields["logical_valid"], &report.LogicalValid)
	json.Unmarshal(fields["restore_ready"], &report.RestoreReady)
	json.Unmarshal(fields["pending_gates"], &report.PendingGates)
	if !report.LogicalValid || report.RestoreReady || len(report.PendingGates) != 4 {
		t.Fatal(output.String())
	}
	entries, err := os.ReadDir(scratch)
	if err != nil || len(entries) != 1 || entries[0].Name() != ".cache.lock" {
		t.Fatal("scratch leak", entries, err)
	}
	before, err := os.ReadFile(file)
	if err != nil || !bytes.Equal(before, data) {
		t.Fatal("source changed", err)
	}
	for _, bad := range [][]string{
		{"--file", file},
		append(append([]string{}, args...), "--master-id", strings.Repeat("a", 32)),
		{"--file", file, "--generation", "1790000000000000123", "--sha256", strings.Repeat("0", 64), "--scratch-dir", scratch},
		append(append([]string{}, args...), "--restore"),
	} {
		output.Reset()
		if err := verifyBackupCommand(bad, &output); err == nil || output.Len() != 0 {
			t.Fatal("invalid input emitted report", output.String(), err)
		}
	}
}

func TestVerifyBackupCommandColdStoreDoesNotInitializeOrChangeRole(t *testing.T) {
	directory := t.TempDir()
	databasePath := filepath.Join(directory, "node.sqlite")
	db, err := sqlitestore.Open(databasePath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	master := strings.Repeat("a", 32)
	data := []byte("{\"kind\":\"header\",\"version\":2,\"generation\":1790000000000000123}\n{\"kind\":\"end\"}\n")
	hash := sha256.Sum256(data)
	raw := db.Database()
	if _, err := raw.Exec("INSERT INTO node_identity(singleton,node_id,`role`,endpoint,private_key,created_at) VALUES (1,?,'Follower','https://historical.test','private-fixture',1)", strings.Repeat("b", 32)); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("INSERT INTO cluster_business_backups(master_id,generation,checksum,size_bytes,chunk_count,state,created_at,updated_at) VALUES (?,1790000000000000123,?,?,1,'ready',1,1)", master, hex.EncodeToString(hash[:]), len(data)); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("INSERT INTO cluster_business_backup_chunks(master_id,generation,chunk_index,payload,created_at) VALUES (?,1790000000000000123,0,?,1)", master, data); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DB_TYPE", "sqlite")
	t.Setenv("SQLITE_PATH", databasePath)
	var output bytes.Buffer
	if err := verifyBackupCommand([]string{"--master-id", master, "--generation", "1790000000000000123", "--scratch-dir", filepath.Join(directory, "scratch")}, &output); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), `"restore_ready":false`) {
		t.Fatal(output.String())
	}
	var role, key string
	if err := raw.QueryRow("SELECT `role`,private_key FROM node_identity WHERE singleton=1").Scan(&role, &key); err != nil || role != "Follower" || key != "private-fixture" {
		t.Fatal("identity changed", role, err)
	}
	missing := filepath.Join(directory, "missing.sqlite")
	t.Setenv("SQLITE_PATH", missing)
	output.Reset()
	if err := verifyBackupCommand([]string{"--master-id", master, "--generation", "1", "--scratch-dir", filepath.Join(directory, "scratch")}, &output); err == nil || output.Len() != 0 {
		t.Fatal(err, output.String())
	}
	if _, err := os.Stat(missing); !os.IsNotExist(err) {
		t.Fatal("inspection created a missing authoritative DB", err)
	}
}
