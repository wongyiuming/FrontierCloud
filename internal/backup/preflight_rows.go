package backup

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/store"
	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
)

type backupColumn struct {
	name, kind                   string
	nullable, generated, primary bool
}

func readBackupColumns(ctx context.Context, db *sql.DB, table string) ([]backupColumn, error) {
	// table is selected exclusively from the immutable compatibility allowlist.
	rows, err := db.QueryContext(ctx, "PRAGMA table_xinfo("+table+")")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []backupColumn
	for rows.Next() {
		var index, notNull, primary, hidden int
		var column backupColumn
		var defaultValue any
		if err := rows.Scan(&index, &column.name, &column.kind, &notNull, &defaultValue, &primary, &hidden); err != nil {
			return nil, err
		}
		column.nullable, column.generated, column.primary = notNull == 0 && primary == 0, hidden != 0, primary != 0
		result = append(result, column)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(result) == 0 {
		return nil, store.ErrBackupState
	}
	return result, nil
}

func stageBackupRow(ctx context.Context, tx *sql.Tx, table string, columns []backupColumn, raw map[string]any) error {
	normalized := make(map[string]any, len(columns))
	var names, placeholders []string
	var values []any
	for _, column := range columns {
		value, exists := raw[column.name]
		if !exists {
			return store.ErrBackupState
		}
		value, err := normalizeBackupCell(column, value)
		if err != nil {
			return err
		}
		normalized[column.name] = value
		if !column.generated {
			names, placeholders = append(names, column.name), append(placeholders, "?")
			values = append(values, value)
		}
	}
	if err := checkBackupRow(table, normalized); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "INSERT INTO "+table+" ("+strings.Join(names, ",")+") VALUES ("+strings.Join(placeholders, ",")+")", values...); err != nil {
		return err
	}
	// SELECT * includes stored generated columns in Python and Go. Verify their
	// declared value, but never insert them or trust them instead of recomputing.
	if table == "ip_auto_ban_events" {
		var actual any
		if err := tx.QueryRowContext(ctx, "SELECT active_ip_address FROM ip_auto_ban_events WHERE id=?", normalized["id"]).Scan(&actual); err != nil {
			return err
		}
		if actual != normalized["active_ip_address"] {
			return store.ErrBackupState
		}
	}
	return nil
}

func normalizeBackupCell(c backupColumn, value any) (any, error) {
	bad := func() (any, error) { return nil, store.ErrBackupState }
	if value == nil {
		if !c.nullable {
			return bad()
		}
		return nil, nil
	}
	kind := strings.ToUpper(c.kind)
	if strings.Contains(kind, "INT") {
		number, ok := value.(json.Number)
		if !ok {
			return bad()
		}
		v, err := strconv.ParseInt(string(number), 10, 64)
		if err != nil || c.primary && c.name == "id" && v <= 0 {
			return bad()
		}
		return v, nil
	}
	if kind == "DATETIME" {
		if wrapped, ok := value.(map[string]any); ok {
			if len(wrapped) != 1 {
				return bad()
			}
			value = wrapped["$datetime"]
		}
		text, ok := value.(string)
		if !ok || len(text) > 64 {
			return bad()
		}
		for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999"} {
			if date, err := time.Parse(layout, text); err == nil && date.Year() >= 1000 && date.Nanosecond()%1000 == 0 {
				return date.UTC().Format("2006-01-02 15:04:05.000000"), nil
			}
		}
		return bad()
	}
	text, ok := value.(string)
	if !ok || len(text) > maxCellBytes || !utf8.ValidString(text) {
		return bad()
	}
	if i := strings.IndexByte(kind, '('); i >= 0 {
		limit, err := strconv.Atoi(strings.TrimSuffix(kind[i+1:], ")"))
		if err != nil || utf8.RuneCountInString(text) > limit {
			return bad()
		}
	}
	if kind == "JSON" {
		if _, err := strictJSON([]byte(text)); err != nil {
			return bad()
		}
	}
	return text, nil
}

func checkBackupRow(table string, r map[string]any) error {
	bad := store.ErrBackupState
	text := func(k string) string { s, _ := r[k].(string); return s }
	num := func(k string) int64 { n, _ := r[k].(int64); return n }
	// IDs used for ownership or cross-record references cannot be arbitrary
	// display text. Historical audit details are deliberately not current links.
	for field, length := range map[string]int{"media_id": 64, "lyric_id": 64, "object_id": 64, "path_locator": 64, "user_id": 32, "recording_id": 32, "storage_member_id": 32, "member_id": 32, "relationship_id": 32, "job_id": 32, "audit_id": 32} {
		if v, exists := r[field]; exists && v != nil && !hexID(text(field), length) {
			return bad
		}
	}
	for _, field := range []string{"media_path", "lyric_path", "relative_path"} {
		if _, exists := r[field]; exists && !safeArtifactPath(text(field)) {
			return bad
		}
	}
	if _, exists := r["path_locator"]; exists {
		hash := sha256.Sum256([]byte(text("media_path")))
		if text("path_locator") != hex.EncodeToString(hash[:]) {
			return bad
		}
	}
	for _, field := range []string{"hidden", "enabled", "storage_enabled", "writable", "matches_verified"} {
		if _, exists := r[field]; exists && (num(field) < 0 || num(field) > 1) {
			return bad
		}
	}
	for _, field := range []string{"size_bytes", "used_bytes", "reserved_bytes", "allocated_bytes", "quota_bytes", "physical_free_bytes", "play_score", "target_count", "failure_count", "success_count", "attack_count", "trigger_count", "observation_count", "matching_count", "worker_slots", "available_slots", "cpu_percent", "memory_available_bytes", "attempts", "generation", "published_generation", "last_success", "lag_seconds", "lease_expires_at"} {
		if _, exists := r[field]; exists && num(field) < 0 {
			return bad
		}
	}
	if _, exists := r["object_kind"]; exists {
		kind, name := text("object_kind"), text("media_path")
		if kind != "audio" && kind != "video" && kind != "lyric" && kind != "directory" {
			return bad
		}
		prefix := strings.Split(name, "/")[0]
		if prefix != "music" && prefix != "vido" && prefix != "lyrics" {
			return bad
		}
		if kind == "audio" && prefix != "music" || kind == "video" && prefix != "vido" || kind == "lyric" && (prefix != "lyrics" || !strings.HasSuffix(name, ".lrc")) {
			return bad
		}
	}
	switch table {
	case "global_media_objects":
		if text("state") != "active" || num("size_bytes") <= 0 || (text("object_kind") != "audio" && text("object_kind") != "video") {
			return bad
		}
	case "cluster_storage_members":
		if num("reserved_bytes") != 0 || num("used_bytes") > num("allocated_bytes") {
			return bad
		}
		if text("member_kind") == "MasterLocal" {
			if text("transport") != "Local" || r["relationship_id"] != nil {
				return bad
			}
		} else if text("member_kind") == "Follower" {
			if (text("transport") != "Direct" && text("transport") != "Relay") || r["relationship_id"] == nil {
				return bad
			}
		} else {
			return bad
		}
		if text("health") != "online" && text("health") != "offline" {
			return bad
		}
	case "cluster_compute_members":
		if num("available_slots") > num("worker_slots") || num("cpu_percent") > 100 {
			return bad
		}
	case "karaoke_users":
		if num("used_bytes") > num("quota_bytes") || (text("status") != "active" && text("status") != "banned") || !validPasswordEncoding(text("password_hash")) || !validBackupUsername(text("username"), text("username_key")) {
			return bad
		}
	case "karaoke_recordings":
		if text("state") != "ready" || num("size_bytes") <= 0 || num("size_bytes") > store.MaxRecordingBytes || !store.ValidRecordingFilename(text("filename")) || !safeArtifactPath(text("filename")) || !store.RecordingContentType(text("content_type")) {
			return bad
		}
		if r["sha256"] != nil && !hexID(text("sha256"), 64) {
			return bad
		}
		var lyrics []store.RecordingLyric
		d := json.NewDecoder(strings.NewReader(text("lyrics")))
		d.DisallowUnknownFields()
		if err := d.Decode(&lyrics); err != nil || lyrics == nil || !store.ValidRecordingMetadata(store.RecordingMetadata{Title: text("title"), Lyrics: lyrics}) {
			return bad
		}
	case "webrtc_observation_summary":
		if num("matching_count") > num("observation_count") {
			return bad
		}
	case "ip_security_projection":
		if num("singleton") != 1 || num("published_generation") > num("generation") {
			return bad
		}
	case "cluster_backup_members":
		if text("checksum") != "" && !hexID(text("checksum"), 64) {
			return bad
		}
		if text("state") == "ready" && (num("generation") <= 0 || text("checksum") == "") {
			return bad
		}
	}
	return nil
}

func validBackupUsername(name, key string) bool {
	if n := utf8.RuneCountInString(name); n < 3 || n > 32 {
		return false
	}
	if !norm.NFKC.IsNormalString(name) || strings.TrimSpace(name) != name || cases.Fold().String(name) != key {
		return false
	}
	for _, r := range name {
		if r != '_' && !unicode.IsLetter(r) && !unicode.IsNumber(r) && !(r >= 0x3400 && r <= 0x9fff) {
			return false
		}
	}
	return true
}

func validPasswordEncoding(s string) bool {
	if len(s) > 256 {
		return false
	}
	parts := strings.Split(s, "$")
	if len(parts) != 6 || parts[0] != "scrypt" || parts[1] != "16384" || parts[2] != "8" || parts[3] != "1" {
		return false
	}
	for i, size := range map[int]int{4: 16, 5: 32} {
		b, err := base64.RawURLEncoding.Strict().DecodeString(parts[i])
		if err != nil || len(b) != size {
			return false
		}
	}
	return true
}

func checkBackupReferences(ctx context.Context, tx *sql.Tx) error {
	// Audits and completed jobs may refer to removed users/media: they are
	// evidence, not ownership. Do not invent active relationships from any ID.
	checks := []struct{ name, query string }{
		{"placement-owner", "SELECT EXISTS(SELECT 1 FROM global_media_objects g LEFT JOIN cluster_storage_members s ON s.member_id=g.storage_member_id WHERE s.member_id IS NULL)"},
		{"placement-identity", "SELECT EXISTS(SELECT 1 FROM global_media_objects g JOIN media_objects o ON o.media_id=g.media_id OR o.path_locator=g.path_locator WHERE o.media_id<>g.media_id OR o.media_path<>g.media_path OR o.object_kind<>g.object_kind)"},
		{"playback-identity", "SELECT EXISTS(SELECT 1 FROM media_playback_stats p LEFT JOIN media_objects o ON o.media_id=p.media_id LEFT JOIN global_media_objects g ON g.media_id=p.media_id WHERE COALESCE(g.media_path,o.media_path,'')<>p.media_path)"},
		{"playback-event", "SELECT EXISTS(SELECT 1 FROM media_playback_events p LEFT JOIN media_objects o ON o.media_id=p.media_id LEFT JOIN global_media_objects g ON g.media_id=p.media_id WHERE o.media_id IS NULL AND g.media_id IS NULL)"},
		{"lyric-link", "SELECT EXISTS(SELECT 1 FROM media_lyric_links l LEFT JOIN media_objects o ON o.media_id=l.media_id LEFT JOIN global_media_objects g ON g.media_id=l.media_id LEFT JOIN media_objects y ON y.media_id=l.lyric_id WHERE COALESCE(g.media_path,o.media_path,'')<>l.media_path OR y.media_id IS NULL OR y.object_kind<>'lyric' OR y.media_path<>l.lyric_path)"},
		{"lyric-payload", "SELECT EXISTS(SELECT 1 FROM media_objects o LEFT JOIN preflight_lyrics l ON ('lyrics/'||l.name)=o.media_path WHERE o.object_kind='lyric' AND l.name IS NULL)"},
		{"recording-owner", "SELECT EXISTS(SELECT 1 FROM karaoke_recordings r LEFT JOIN karaoke_users u ON u.user_id=r.user_id LEFT JOIN cluster_storage_members s ON s.member_id=r.storage_member_id WHERE u.user_id IS NULL OR s.member_id IS NULL)"},
		{"user-capacity", "SELECT EXISTS(SELECT 1 FROM karaoke_users u LEFT JOIN (SELECT user_id,SUM(size_bytes) AS used FROM karaoke_recordings GROUP BY user_id) r ON r.user_id=u.user_id WHERE u.used_bytes<>COALESCE(r.used,0))"},
		// Storage can include uncatalogued physical files. Require a lower bound,
		// not a fabricated equality or automatic capacity refund.
		{"storage-capacity", "SELECT EXISTS(SELECT 1 FROM cluster_storage_members s LEFT JOIN (SELECT storage_member_id,SUM(size_bytes) AS used FROM (SELECT storage_member_id,size_bytes FROM global_media_objects UNION ALL SELECT storage_member_id,size_bytes FROM karaoke_recordings) GROUP BY storage_member_id) p ON p.storage_member_id=s.member_id WHERE s.used_bytes<COALESCE(p.used,0))"},
		{"local-identity", "SELECT COUNT(*)>1 FROM cluster_storage_members WHERE member_kind='MasterLocal'"},
	}
	for _, check := range checks {
		var invalid bool
		if err := tx.QueryRowContext(ctx, check.query).Scan(&invalid); err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return fmt.Errorf("%w: %s check failed", store.ErrBackupState, check.name)
		}
		if invalid {
			return fmt.Errorf("%w: %s", store.ErrBackupState, check.name)
		}
	}
	return nil
}
