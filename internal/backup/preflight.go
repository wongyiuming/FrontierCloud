package backup

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/store"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	"github.com/wongyiuming/FrontierCloud/migrations"
)

const MaxRecordBytes = 16 * 1024 * 1024
const maxCellBytes = 4 * 1024 * 1024

type Expectation struct {
	Generation int64
	Checksum   string
	Bytes      int64
}

// Report is proof of the stated logical checks ONLY. No private identity or
// relationship credential exists in v2, and media/recording bytes are absent.
// This report must never be treated as permission to overwrite or promote.
type Report struct {
	Generation   int64            `json:"generation"`
	Checksum     string           `json:"checksum"`
	Bytes        int64            `json:"bytes"`
	Rows         map[string]int64 `json:"rows"`
	Lyrics       int64            `json:"lyrics"`
	LogicalValid bool             `json:"logical_valid"`
	RestoreReady bool             `json:"restore_ready"`
	PendingGates []string         `json:"pending_gates"`
}

// InspectReady is a local, read-only maintenance operation, with no HTTP route.
// Neither partial parser output nor partial SQL reads can escape as a report.
func InspectReady(ctx context.Context, repo store.BackupRepository, master string, generation int64, scratch string) (Report, error) {
	if repo == nil {
		return Report{}, store.ErrBackupState
	}
	var result Report
	_, err := repo.ReadReadyBackup(ctx, master, generation, func(m store.BackupManifest, reader io.Reader) error {
		var err error
		result, err = Preflight(ctx, reader, Expectation{m.Generation, m.Checksum, m.Bytes}, scratch)
		return err
	})
	if err != nil {
		return Report{}, err
	}
	return result, nil
}

// Preflight stages untrusted rows in an isolated native SQLite scratch database.
// The shared schema supplies columns/constraints; table names are the fixed v2
// allowlist, and values are parameters. No authoritative DB or lyric is opened.
func Preflight(ctx context.Context, reader io.Reader, expected Expectation, directory string) (result Report, resultErr error) {
	defer func() {
		if resultErr != nil {
			result = Report{}
		}
	}()
	if reader == nil || directory == "" || expected.Generation <= 0 || !hexID(expected.Checksum, 64) || expected.Bytes <= 0 || expected.Bytes > int64(store.MaxBackupChunkIndex+1)*store.MaxBackupChunk {
		return Report{}, store.ErrBackupState
	}
	if err := ctx.Err(); err != nil {
		return Report{}, err
	}
	directory, err := filepath.Abs(directory)
	if err != nil {
		return Report{}, err
	}
	if err := os.MkdirAll(directory, 0700); err != nil {
		return Report{}, err
	}
	info, err := os.Lstat(directory)
	if err != nil {
		return Report{}, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return Report{}, store.ErrBackupState
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		return Report{}, err
	}
	defer func() { resultErr = errors.Join(resultErr, root.Close()) }()
	var identifier [16]byte
	if _, err := rand.Read(identifier[:]); err != nil {
		return Report{}, err
	}
	name := "preflight-" + hex.EncodeToString(identifier[:])
	if err := root.Mkdir(name, 0700); err != nil {
		return Report{}, err
	}
	// Exact generated child under a rooted directory; never delete the caller's
	// directory or any pre-existing artifact, even after a validation failure.
	defer func() { resultErr = errors.Join(resultErr, root.RemoveAll(name)) }()
	f, err := root.OpenFile(name+"/check.sqlite", os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		return Report{}, err
	}
	if err := f.Close(); err != nil {
		return Report{}, err
	}
	db, err := sqlitestore.Open(filepath.Join(directory, name, "check.sqlite"))
	if err != nil {
		return Report{}, err
	}
	defer func() { resultErr = errors.Join(resultErr, db.Close()) }()
	raw := db.Database()
	raw.SetMaxOpenConns(1)
	for _, statement := range []string{"PRAGMA journal_mode=DELETE", "PRAGMA cache_size=-4096", "PRAGMA temp_store=FILE", "PRAGMA mmap_size=0"} {
		if _, err := raw.ExecContext(ctx, statement); err != nil {
			return Report{}, err
		}
	}
	statements, err := migrations.Statements("sqlite")
	if err != nil {
		return Report{}, err
	}
	for _, statement := range statements {
		if _, err := raw.ExecContext(ctx, statement); err != nil {
			return Report{}, err
		}
	}
	if _, err := raw.ExecContext(ctx, "CREATE TABLE preflight_lyrics (name TEXT NOT NULL PRIMARY KEY)"); err != nil {
		return Report{}, err
	}
	tables := make(map[string][]backupColumn)
	report := Report{Generation: expected.Generation, Checksum: expected.Checksum, Bytes: expected.Bytes, Rows: make(map[string]int64), PendingGates: []string{"maintenance-fence", "identity-and-relationship-remapping", "physical-ownership-proof", "atomic-publication-and-rollback"}}
	for _, table := range store.BusinessBackupTables() {
		columns, err := readBackupColumns(ctx, raw, table)
		if err != nil {
			return Report{}, err
		}
		tables[table], report.Rows[table] = columns, 0
	}
	tx, err := raw.BeginTx(ctx, nil)
	if err != nil {
		return Report{}, err
	}
	defer tx.Rollback()
	digest := sha256.New()
	input := &countedInput{ctx: ctx, reader: io.TeeReader(io.LimitReader(reader, expected.Bytes+1), digest)}
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 64*1024), MaxRecordBytes+1)
	scanner.Split(jsonLines)
	header, ended := false, false
	var line int64
	for scanner.Scan() {
		line++
		if err := ctx.Err(); err != nil {
			return Report{}, err
		}
		bad := func() (Report, error) { return Report{}, fmt.Errorf("%w: record %d", store.ErrBackupState, line) }
		value, err := strictJSON(scanner.Bytes())
		if err != nil {
			return bad()
		}
		record, ok := value.(map[string]any)
		if !ok || ended {
			return bad()
		}
		kind, _ := record["kind"].(string)
		if !header {
			if kind != "header" || len(record) != 3 || record["version"] != json.Number("2") || record["generation"] != json.Number(strconv.FormatInt(expected.Generation, 10)) {
				return bad()
			}
			header = true
			continue
		}
		switch kind {
		case "end":
			if len(record) != 1 {
				return bad()
			}
			ended = true
		case "row":
			table, _ := record["table"].(string)
			row, ok := record["value"].(map[string]any)
			columns, allowed := tables[table]
			if len(record) != 3 || !ok || !allowed || len(row) != len(columns) {
				return bad()
			}
			if err := stageBackupRow(ctx, tx, table, columns, row); err != nil {
				if ctx.Err() != nil {
					return Report{}, ctx.Err()
				}
				// Do not echo untrusted rows, paths, passwords or SQL errors.
				return bad()
			}
			report.Rows[table]++
		case "lyric":
			name, nameOK := record["name"].(string)
			payload, payloadOK := record["payload"].(string)
			if len(record) != 3 || !nameOK || !payloadOK || !safeArtifactPath(name) || !strings.HasSuffix(name, ".lrc") || len(payload) > base64.StdEncoding.EncodedLen(int(MaxLyricBytes)) || strings.ContainsAny(payload, "\r\n") {
				return bad()
			}
			decoded, err := base64.StdEncoding.Strict().DecodeString(payload)
			if err != nil || int64(len(decoded)) > MaxLyricBytes {
				return bad()
			}
			if _, err := tx.ExecContext(ctx, "INSERT INTO preflight_lyrics(name) VALUES (?)", name); err != nil {
				if ctx.Err() != nil {
					return Report{}, ctx.Err()
				}
				return bad()
			}
			report.Lyrics++
		default:
			return bad()
		}
	}
	if err := scanner.Err(); err != nil {
		return Report{}, fmt.Errorf("%w: unreadable record: %w", store.ErrBackupState, err)
	}
	if !header || !ended || input.bytes != expected.Bytes || hex.EncodeToString(digest.Sum(nil)) != expected.Checksum {
		return Report{}, store.ErrBackupState
	}
	if err := checkBackupReferences(ctx, tx); err != nil {
		return Report{}, err
	}
	if err := tx.Commit(); err != nil {
		return Report{}, err
	}
	report.LogicalValid = true
	return report, nil
}

type countedInput struct {
	ctx    context.Context
	reader io.Reader
	bytes  int64
}

func (r *countedInput) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	n, err := r.reader.Read(p)
	r.bytes += int64(n)
	return n, err
}
func jsonLines(data []byte, eof bool) (int, []byte, error) {
	if i := bytes.IndexByte(data, '\n'); i >= 0 {
		if i+1 > MaxRecordBytes {
			return 0, nil, store.ErrBackupState
		}
		return i + 1, data[:i], nil
	}
	if eof && len(data) != 0 {
		return 0, nil, store.ErrBackupState
	}
	return 0, nil, nil
}

// encoding/json's map decoder silently overwrites duplicate keys. Token parsing
// rejects them at every level, bounds nesting and preserves exact integers.
func strictJSON(data []byte) (any, error) {
	if !utf8.Valid(data) {
		return nil, store.ErrBackupState
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	v, err := jsonValue(d, 0)
	if err != nil {
		return nil, err
	}
	if _, err := d.Token(); err != io.EOF {
		return nil, store.ErrBackupState
	}
	return v, nil
}
func jsonValue(d *json.Decoder, depth int) (any, error) {
	if depth > 32 {
		return nil, store.ErrBackupState
	}
	t, err := d.Token()
	if err != nil {
		return nil, err
	}
	if n, ok := t.(json.Number); ok {
		f, err := n.Float64()
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
			return nil, store.ErrBackupState
		}
	}
	delim, ok := t.(json.Delim)
	if !ok {
		return t, nil
	}
	switch delim {
	case '{':
		m := make(map[string]any)
		for d.More() {
			key, err := d.Token()
			if err != nil {
				return nil, err
			}
			name, ok := key.(string)
			if !ok {
				return nil, store.ErrBackupState
			}
			if _, found := m[name]; found {
				return nil, store.ErrBackupState
			}
			v, err := jsonValue(d, depth+1)
			if err != nil {
				return nil, err
			}
			m[name] = v
		}
		end, err := d.Token()
		if err != nil || end != json.Delim('}') {
			return nil, store.ErrBackupState
		}
		return m, nil
	case '[':
		a := []any{}
		for d.More() {
			v, err := jsonValue(d, depth+1)
			if err != nil {
				return nil, err
			}
			a = append(a, v)
		}
		end, err := d.Token()
		if err != nil || end != json.Delim(']') {
			return nil, store.ErrBackupState
		}
		return a, nil
	}
	return nil, store.ErrBackupState
}
func hexID(s string, n int) bool {
	if len(s) != n {
		return false
	}
	for _, c := range s {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}
func safeArtifactPath(s string) bool {
	if len(s) == 0 || len(s) > 4096 || path.Clean(s) != s || strings.ContainsAny(s, "\\:\x00\r\n") || strings.HasPrefix(s, "/") {
		return false
	}
	for _, p := range strings.Split(s, "/") {
		if strings.HasPrefix(p, ".") || len(p) > 255 {
			return false
		}
	}
	return true
}
