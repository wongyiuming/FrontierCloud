package httpapi

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"strconv"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestNodeBackupSignedBoundedBinaryChunksCommitAbortAndRoleFences(t *testing.T) {
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	master := nativeRecordingFixture(t, "https://backup-master.test", "Master", transport)
	follower := nativeRecordingFixture(t, "https://backup-follower.test", "Follower", transport)
	pack, err := follower.control.CreatePair(ctx, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	id, err := master.control.ImportPair(ctx, pack, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	rel, err := master.db.Nodes().Relationship(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Backup.Enabled = true
	if err := master.db.Pool().ConfigureMember(ctx, follower.id, cfg, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := master.control.Tick(ctx, rel); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		origin, body string
		code         int
	}{
		{"http://backup-follower.test", "{}", 403},
		{"https://backup-follower.test", "not-json", 401},
		{"https://backup-follower.test", strings.Repeat("x", node.MaxControlBytes+1), 413},
	} {
		w := request(follower.router, "POST", test.origin+"/internal/v1/backup/begin", test.body)
		if w.Code != test.code {
			t.Fatal("unauthenticated backup", w.Code, w.Body.String())
		}
	}
	call := func(operation string, value map[string]any) map[string]any {
		t.Helper()
		result, err := master.control.Call(ctx, rel, "/internal/v1/backup/"+operation, value)
		if err != nil {
			t.Fatal(operation, err)
		}
		return result
	}
	if result := call("begin", map[string]any{"generation": 100}); result["status"] != "receiving" {
		t.Fatal(result)
	}
	for _, test := range []struct {
		operation string
		value     map[string]any
	}{
		{"begin", map[string]any{"generation": 0}},
		{"chunk", map[string]any{"generation": 100, "chunk_index": 0}},
		{"chunk", map[string]any{"generation": 100, "chunk_index": 0, "chunk": "not base64"}},
		{"chunk", map[string]any{"generation": 100, "chunk_index": 0, "chunk": "QQ==\n"}},
		{"chunk", map[string]any{"generation": 100, "chunk_index": -1, "chunk": "QQ=="}},
		{"chunk", map[string]any{"generation": 100, "chunk_index": 0, "chunk": base64.StdEncoding.EncodeToString(make([]byte, store.MaxBackupChunk+1))}},
		{"commit", map[string]any{"generation": 100, "checksum": "invalid"}},
	} {
		if _, err := master.control.Call(ctx, rel, "/internal/v1/backup/"+test.operation, test.value); err == nil || !strings.Contains(err.Error(), "control status 400") {
			t.Fatal("invalid backup message accepted", test.operation, err)
		}
	}
	data := append(bytes.Repeat([]byte("备份\x00opaque"), 16_000), []byte("\xff\x01")...)
	if len(data) <= store.MaxBackupChunk || len(data) > 2*store.MaxBackupChunk {
		t.Fatal("test payload bounds", len(data))
	}
	for index, chunk := range [][]byte{data[:store.MaxBackupChunk], data[store.MaxBackupChunk:]} {
		for range 2 {
			result := call("chunk", map[string]any{"generation": 100, "chunk_index": index, "chunk": base64.StdEncoding.EncodeToString(chunk)})
			if result["status"] != "receiving" || result["bytes"] != json.Number(strconv.Itoa(len(chunk))) {
				t.Fatal("binary chunk receipt", result)
			}
		}
	}
	hash := sha256.Sum256(data)
	checksum := hex.EncodeToString(hash[:])
	for range 2 {
		result := call("commit", map[string]any{"generation": 100, "checksum": checksum})
		if result["status"] != "ready" || result["bytes"] != json.Number(strconv.Itoa(len(data))) {
			t.Fatal("manifest receipt", result)
		}
	}
	var stored []byte
	if err := follower.db.Database().QueryRow("SELECT payload FROM cluster_business_backup_chunks WHERE master_id=? AND generation=100 AND chunk_index=1", master.id).Scan(&stored); err != nil || !bytes.Equal(stored, data[store.MaxBackupChunk:]) {
		t.Fatal("binary data changed", err)
	}
	for _, generation := range []int64{101, 102} {
		call("begin", map[string]any{"generation": generation})
		call("chunk", map[string]any{"generation": generation, "chunk_index": 0, "chunk": "QQ=="})
	}
	result := call("abort", map[string]any{"generation": 999})
	if result["status"] != "failed" || result["generation"] != json.Number("999") || result["aborted_generations"] != json.Number("2") {
		t.Fatal("abort contract", result)
	}
	if result := call("abort", map[string]any{"generation": 999}); result["status"] != "clean" {
		t.Fatal("abort replay", result)
	}
	// Python time_ns generations exceed float64's exact integer range.
	const nanosecondGeneration int64 = 1_790_000_000_000_000_123
	call("begin", map[string]any{"generation": nanosecondGeneration})
	result = call("abort", map[string]any{"generation": nanosecondGeneration})
	if result["generation"] != json.Number(strconv.FormatInt(nanosecondGeneration, 10)) || result["aborted_generations"] != json.Number("1") {
		t.Fatal("generation precision lost", result)
	}
	var state string
	if err := follower.db.Database().QueryRow("SELECT state FROM cluster_business_backups WHERE master_id=? AND generation=100", master.id).Scan(&state); err != nil || state != "ready" {
		t.Fatal("abort destroyed ready backup", state, err)
	}
	upstream, err := follower.db.Nodes().Relationship(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := follower.control.Call(ctx, upstream, "/internal/v1/backup/begin", map[string]any{"generation": 100}); err == nil || !strings.Contains(err.Error(), "control status 403") {
		t.Fatal("Follower wrote backup into Master", err)
	}
	if _, err := follower.db.Database().Exec("UPDATE node_identity SET `role`='Standalone' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	if _, err := master.control.Call(ctx, rel, "/internal/v1/backup/begin", map[string]any{"generation": 103}); err == nil || !strings.Contains(err.Error(), "control status 403") {
		t.Fatal("stale Follower role accepted", err)
	}
	if _, err := follower.db.Database().Exec("UPDATE node_identity SET `role`='Follower' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	if _, err := follower.db.Database().Exec("UPDATE node_relationships SET state='revoked' WHERE relationship_id=?", id); err != nil {
		t.Fatal(err)
	}
	if _, err := master.control.Call(ctx, rel, "/internal/v1/backup/abort", map[string]any{"generation": 100}); err == nil || !strings.Contains(err.Error(), "control status 401") {
		t.Fatal("revoked upstream accepted", err)
	}
}
