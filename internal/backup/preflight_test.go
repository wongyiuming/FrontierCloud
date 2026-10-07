package backup

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

const testGeneration int64 = 1790000000000000123

func preflightRecords() []map[string]any {
	id, lyric, owner, user := strings.Repeat("a", 64), strings.Repeat("b", 64), strings.Repeat("c", 32), strings.Repeat("d", 32)
	date := map[string]any{"$datetime": "2026-10-02T12:34:56.123456"}
	locator := func(s string) string { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:]) }
	row := func(table string, value map[string]any) map[string]any {
		return map[string]any{"kind": "row", "table": table, "value": value}
	}
	return []map[string]any{
		{"kind": "header", "version": 2, "generation": testGeneration},
		row("media_objects", map[string]any{"media_id": id, "object_kind": "audio", "media_path": "music/现场/歌曲.mp3", "path_locator": locator("music/现场/歌曲.mp3"), "created_at": date, "updated_at": date}),
		row("media_objects", map[string]any{"media_id": lyric, "object_kind": "lyric", "media_path": "lyrics/现场.lrc", "path_locator": locator("lyrics/现场.lrc"), "created_at": date, "updated_at": date}),
		row("media_playback_stats", map[string]any{"media_id": id, "media_path": "music/现场/歌曲.mp3", "play_score": int64(9007199254740993), "preference": 7, "created_at": date, "updated_at": date}),
		row("media_lyric_links", map[string]any{"media_id": id, "media_path": "music/现场/歌曲.mp3", "lyric_id": lyric, "lyric_path": "lyrics/现场.lrc", "created_at": date, "updated_at": date}),
		row("global_media_objects", map[string]any{"media_id": id, "storage_member_id": owner, "object_id": id, "media_path": "music/现场/歌曲.mp3", "path_locator": locator("music/现场/歌曲.mp3"), "object_kind": "audio", "size_bytes": 100, "etag": "\"native\"", "state": "active", "created_at": 1, "updated_at": 2}),
		row("cluster_storage_members", map[string]any{"member_id": owner, "relationship_id": nil, "member_kind": "MasterLocal", "transport": "Local", "storage_enabled": 1, "allocated_bytes": store.GiB, "used_bytes": 128, "reserved_bytes": 0, "physical_free_bytes": 1000, "health": "online", "writable": 1, "updated_at": 2}),
		row("karaoke_users", map[string]any{"user_id": user, "username": "现场用户", "username_key": "现场用户", "password_hash": "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA$pBvZM-z6uzE8Uh2oYD3DkPSoiN84wwWjaC8fzHz1HF8", "status": "active", "quota_bytes": 100, "used_bytes": 28, "created_at": 1, "updated_at": 2}),
		row("karaoke_recordings", map[string]any{"recording_id": strings.Repeat("e", 32), "user_id": user, "storage_member_id": owner, "filename": "现场.webm", "content_type": "audio/webm", "size_bytes": 28, "sha256": strings.Repeat("f", 64), "state": "ready", "title": "现场", "lyrics": "[{\"time\":0.25,\"text\":\"你好\"}]", "created_at": 1, "updated_at": 2}),
		row("ip_auto_ban_events", map[string]any{"id": 1, "ip_address": "192.0.2.1", "trigger_count": 1, "window_started_at": date, "banned_at": date, "expires_at": date, "last_method": nil, "last_path": nil, "user_agent": nil, "ban_kind": "auto", "reason": nil, "created_by_session_hash": nil, "status": "active", "released_at": nil, "released_by_session_hash": nil, "active_ip_address": "192.0.2.1"}),
		row("ip_security_projection", map[string]any{"singleton": 1, "generation": 2, "published_generation": 1}),
		{"kind": "lyric", "name": "现场.lrc", "payload": base64.StdEncoding.EncodeToString([]byte("[00:01]你好\n"))},
		{"kind": "end"},
	}
}
func encodedPreflight(records []map[string]any) []byte {
	var b bytes.Buffer
	for _, record := range records {
		if err := json.NewEncoder(&b).Encode(record); err != nil {
			panic(err)
		}
	}
	return b.Bytes()
}
func expectedPreflight(data []byte) Expectation {
	hash := sha256.Sum256(data)
	return Expectation{testGeneration, hex.EncodeToString(hash[:]), int64(len(data))}
}
func preflightRow(records []map[string]any, table string) map[string]any {
	for _, record := range records {
		if record["table"] == table {
			return record["value"].(map[string]any)
		}
	}
	panic("missing fixture row")
}
func checkScratchEmpty(t *testing.T, directory string) {
	t.Helper()
	entries, err := os.ReadDir(directory)
	if err != nil || len(entries) != 1 || entries[0].Name() != cacheLock {
		t.Fatal("scratch leaked", entries, err)
	}
}

func TestBackupPreflightExactGenerationTypedRowsGeneratedColumnReferencesAndCleanup(t *testing.T) {
	for _, plainDate := range []bool{false, true} {
		records := preflightRecords()
		if plainDate {
			preflightRow(records, "media_objects")["created_at"] = "2026-10-02 12:34:56.123456"
		}
		data := encodedPreflight(records)
		directory := t.TempDir()
		report, err := Preflight(context.Background(), bytes.NewReader(data), expectedPreflight(data), directory)
		if err != nil || !report.LogicalValid || report.RestoreReady || report.Generation != testGeneration || report.Rows["media_objects"] != 2 || len(report.Rows) != 23 || report.Lyrics != 1 || len(report.PendingGates) != 4 {
			t.Fatal(report, err)
		}
		checkScratchEmpty(t, directory)
	}
}

func TestBackupPreflightExistingVideoRootContract(t *testing.T) {
	for _, prefix := range []string{"vido", "movies"} {
		records := preflightRecords()
		name := prefix + "/导演/电影.mp4"
		hash := sha256.Sum256([]byte(name))
		for _, table := range []string{"media_objects", "global_media_objects"} {
			row := preflightRow(records, table)
			row["object_kind"], row["media_path"], row["path_locator"] = "video", name, hex.EncodeToString(hash[:])
		}
		preflightRow(records, "media_playback_stats")["media_path"] = name
		preflightRow(records, "media_lyric_links")["media_path"] = name
		data := encodedPreflight(records)
		report, err := Preflight(context.Background(), bytes.NewReader(data), expectedPreflight(data), t.TempDir())
		if prefix == "vido" {
			if err != nil || !report.LogicalValid {
				t.Fatal("valid legacy video root rejected", report, err)
			}
		} else if !errors.Is(err, store.ErrBackupState) {
			t.Fatal("invented video root accepted", err)
		}
	}
}

func TestBackupPreflightRejectsSchemaIdentityCapacityAndUnsafePayloads(t *testing.T) {
	cases := map[string]func([]map[string]any) []map[string]any{
		"unknown-table": func(r []map[string]any) []map[string]any { r[1]["table"] = "node_identity"; return r },
		"missing-column": func(r []map[string]any) []map[string]any {
			delete(preflightRow(r, "karaoke_users"), "status")
			return r
		},
		"unknown-column": func(r []map[string]any) []map[string]any {
			preflightRow(r, "karaoke_users")["private_key"] = "secret"
			return r
		},
		"null-required": func(r []map[string]any) []map[string]any {
			preflightRow(r, "media_objects")["media_id"] = nil
			return r
		},
		"fractional-integer": func(r []map[string]any) []map[string]any {
			preflightRow(r, "global_media_objects")["size_bytes"] = 1.25
			return r
		},
		"overflow-integer": func(r []map[string]any) []map[string]any {
			preflightRow(r, "global_media_objects")["size_bytes"] = json.Number("9223372036854775808")
			return r
		},
		"invalid-datetime": func(r []map[string]any) []map[string]any {
			preflightRow(r, "media_objects")["created_at"] = map[string]any{"$datetime": "not-a-date"}
			return r
		},
		"invalid-locator": func(r []map[string]any) []map[string]any {
			preflightRow(r, "media_objects")["path_locator"] = strings.Repeat("1", 64)
			return r
		},
		"duplicate-identity": func(r []map[string]any) []map[string]any { return append(r[:len(r)-1], r[1], r[len(r)-1]) },
		"overlong-field": func(r []map[string]any) []map[string]any {
			preflightRow(r, "karaoke_users")["username"] = strings.Repeat("a", 65)
			return r
		},
		"duplicate-lyric":     func(r []map[string]any) []map[string]any { return append(r[:len(r)-1], r[len(r)-2], r[len(r)-1]) },
		"missing-lyric":       func(r []map[string]any) []map[string]any { return append(r[:len(r)-2], r[len(r)-1]) },
		"lyric-traversal":     func(r []map[string]any) []map[string]any { r[len(r)-2]["name"] = "../escape.lrc"; return r },
		"lyric-windows-drive": func(r []map[string]any) []map[string]any { r[len(r)-2]["name"] = "C:/escape.lrc"; return r },
		"lyric-private":       func(r []map[string]any) []map[string]any { r[len(r)-2]["name"] = ".secret.lrc"; return r },
		"bad-base64":          func(r []map[string]any) []map[string]any { r[len(r)-2]["payload"] = "YR=="; return r },
		"generated-forgery": func(r []map[string]any) []map[string]any {
			preflightRow(r, "ip_auto_ban_events")["active_ip_address"] = "192.0.2.2"
			return r
		},
		"reserved-capacity": func(r []map[string]any) []map[string]any {
			preflightRow(r, "cluster_storage_members")["reserved_bytes"] = 1
			return r
		},
		"used-underflows-placements": func(r []map[string]any) []map[string]any {
			preflightRow(r, "cluster_storage_members")["used_bytes"] = 127
			return r
		},
		"user-accounting-mismatch": func(r []map[string]any) []map[string]any {
			preflightRow(r, "karaoke_users")["used_bytes"] = 27
			return r
		},
		"unresolved-recording": func(r []map[string]any) []map[string]any {
			preflightRow(r, "karaoke_recordings")["state"] = "pending"
			return r
		},
		"missing-user": func(r []map[string]any) []map[string]any {
			preflightRow(r, "karaoke_recordings")["user_id"] = strings.Repeat("f", 32)
			return r
		},
		"missing-owner": func(r []map[string]any) []map[string]any {
			preflightRow(r, "global_media_objects")["storage_member_id"] = strings.Repeat("f", 32)
			return r
		},
		"wrong-link-identity": func(r []map[string]any) []map[string]any {
			preflightRow(r, "media_lyric_links")["lyric_id"] = strings.Repeat("f", 64)
			return r
		},
		"duplicate-json-cell-key": func(r []map[string]any) []map[string]any {
			preflightRow(r, "karaoke_recordings")["lyrics"] = "[{\"time\":0,\"time\":1,\"text\":\"x\"}]"
			return r
		},
		"excessive-password-work": func(r []map[string]any) []map[string]any {
			preflightRow(r, "karaoke_users")["password_hash"] = "scrypt$99999999$32$1$salt$hash"
			return r
		},
		"username-key-forgery": func(r []map[string]any) []map[string]any {
			preflightRow(r, "karaoke_users")["username_key"] = "another_user"
			return r
		},
		"preference-constraint": func(r []map[string]any) []map[string]any {
			preflightRow(r, "media_playback_stats")["preference"] = 501
			return r
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			data := encodedPreflight(mutate(preflightRecords()))
			directory := t.TempDir()
			report, err := Preflight(context.Background(), bytes.NewReader(data), expectedPreflight(data), directory)
			if !errors.Is(err, store.ErrBackupState) || report.LogicalValid || report.RestoreReady {
				t.Fatal(report, err)
			}
			checkScratchEmpty(t, directory)
		})
	}
}

func TestBackupPreflightStrictFramingIntegrityAndCancellation(t *testing.T) {
	valid := encodedPreflight(preflightRecords())
	cases := map[string][]byte{
		"duplicate-header-key": []byte("{\"kind\":\"header\",\"version\":1,\"version\":2,\"generation\":1790000000000000123}\n{\"kind\":\"end\"}\n"),
		"missing-footer":       valid[:bytes.LastIndex(valid, []byte("{\"kind\":\"end\"}"))],
		"post-footer":          append(bytes.Clone(valid), []byte("{\"kind\":\"end\"}\n")...),
		"no-final-newline":     valid[:len(valid)-1],
		"invalid-utf8":         bytes.Replace(valid, []byte("现场.webm"), []byte{0xff}, 1),
		"nested-header":        []byte("{\"kind\":\"header\",\"version\":2,\"generation\":1790000000000000123}\n{\"kind\":\"header\"}\n"),
		"oversize-record":      append([]byte("{\"kind\":\"header\",\"data\":\""), []byte(strings.Repeat("a", MaxRecordBytes))...),
		"extra-json-value":     []byte("{\"kind\":\"end\"} {}\n"),
		"blank-line":           append(bytes.Clone(valid), '\n'),
	}
	for name, data := range cases {
		t.Run(name, func(t *testing.T) {
			directory := t.TempDir()
			if _, err := Preflight(context.Background(), bytes.NewReader(data), expectedPreflight(data), directory); !errors.Is(err, store.ErrBackupState) {
				t.Fatal(err)
			}
			checkScratchEmpty(t, directory)
		})
	}
	for _, change := range []func(*Expectation){func(e *Expectation) { e.Generation++ }, func(e *Expectation) { e.Bytes-- }, func(e *Expectation) { e.Checksum = strings.Repeat("0", 64) }} {
		e := expectedPreflight(valid)
		change(&e)
		if _, err := Preflight(context.Background(), bytes.NewReader(valid), e, t.TempDir()); !errors.Is(err, store.ErrBackupState) {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	directory := t.TempDir()
	reader := &cancelPreflightReader{Reader: bytes.NewReader(valid), cancel: cancel}
	if _, err := Preflight(ctx, reader, expectedPreflight(valid), directory); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	checkScratchEmpty(t, directory)
}

type cancelPreflightReader struct {
	io.Reader
	cancel context.CancelFunc
}

func TestBackupPreflightBoundedParserAndScratchSafety(t *testing.T) {
	for _, raw := range []string{
		strings.Repeat("[", 34) + "0" + strings.Repeat("]", 34),
		`{"outer":{"same":1,"same":2}}`,
		`{"float":1e999}`, `{"x":NaN}`, `{"x":true} false`,
	} {
		if _, err := strictJSON([]byte(raw)); err == nil {
			t.Fatal("unsafe JSON accepted")
		}
	}
	data := encodedPreflight(preflightRecords())
	if _, err := Preflight(context.Background(), bytes.NewReader(data), expectedPreflight(data), ""); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("implicit scratch workspace", err)
	}
	target, link := t.TempDir(), t.TempDir()+"/link"
	if err := os.Symlink(target, link); err != nil {
		t.Skip("symlink unavailable", err)
	}
	if _, err := Preflight(context.Background(), bytes.NewReader(data), expectedPreflight(data), link); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("symlink scratch accepted", err)
	}
	if entries, err := os.ReadDir(target); err != nil || len(entries) != 0 {
		t.Fatal("rejected symlink scratch was modified", entries, err)
	}
}

func (r *cancelPreflightReader) Read(p []byte) (int, error) {
	n, err := r.Reader.Read(p)
	r.cancel()
	return n, err
}
