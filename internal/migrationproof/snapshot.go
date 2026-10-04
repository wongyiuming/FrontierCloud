// Package migrationproof implements a read-only, same-MySQL offline migration
// inventory. It never repairs data, initializes a store, or grants runtime
// authority. Callers MUST hold an authoritative MySQL read lock and stop all
// legacy filesystem writers for the entire inventory/publication interval.
package migrationproof

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
)

const Format = "frontiercloud-offline-mysql-master-v1"

type Table struct {
	Name   string `json:"name"`
	DDL    string `json:"ddl_sha256"`
	Rows   int64  `json:"rows"`
	Digest string `json:"rows_sha256"`
}
type File struct {
	Path   string `json:"path"`
	Bytes  int64  `json:"bytes"`
	Mode   uint32 `json:"mode"`
	Digest string `json:"sha256"`
}
type Snapshot struct {
	Format   string  `json:"format"`
	NodeID   string  `json:"node_id"`
	Endpoint string  `json:"endpoint"`
	Database string  `json:"database"`
	Version  string  `json:"mysql_version"`
	Tables   []Table `json:"tables"`
	Data     []File  `json:"data"`
	Secrets  []File  `json:"secrets"`
}

var identifier = regexp.MustCompile(`^[A-Za-z0-9_]+$`)
var nodeIdentifier = regexp.MustCompile(`^[0-9a-f]{32}$`)

// Capture hashes every persisted row (NULLs and binary values included), every
// managed file including unreferenced history, and every secret. SHOW CREATE
// retains indexes, constraints and next AUTO_INCREMENT; no tables are omitted.
func Capture(ctx context.Context, conn *sql.Conn, database, data, secrets string) (Snapshot, error) {
	s := Snapshot{Format: Format, Database: database}
	if !identifier.MatchString(database) {
		return s, errors.New("invalid database identifier")
	}
	if err := conn.QueryRowContext(ctx, "SELECT VERSION()").Scan(&s.Version); err != nil {
		return s, err
	}
	var role string
	if err := conn.QueryRowContext(ctx, "SELECT node_id, `role`, endpoint FROM node_identity WHERE singleton=1").Scan(&s.NodeID, &role, &s.Endpoint); err != nil {
		return s, err
	}
	if role != "Master" || !nodeIdentifier.MatchString(s.NodeID) || !strings.HasPrefix(s.Endpoint, "https://") {
		return s, errors.New("existing HTTPS Master required")
	}
	var generation int
	if err := conn.QueryRowContext(ctx, "SELECT generation FROM frontiercloud_schema WHERE singleton=1").Scan(&generation); err != nil || generation != 2 {
		return s, errors.New("schema generation 2 required")
	}
	// These objects have behavior outside the supported native schema. Preserve
	// rather than silently omit them: require a separately reviewed migration.
	for _, query := range []string{
		"SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=?",
		"SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=?",
		"SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=?",
	} {
		var count int
		if err := conn.QueryRowContext(ctx, query, database).Scan(&count); err != nil {
			return s, err
		}
		if count != 0 {
			return s, errors.New("custom database objects require explicit migration review")
		}
	}
	rows, err := conn.QueryContext(ctx, "SELECT TABLE_NAME, COALESCE(ENGINE,''), TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=? ORDER BY TABLE_NAME", database)
	if err != nil {
		return s, err
	}
	var names []string
	for rows.Next() {
		var name, engine, kind string
		if err = rows.Scan(&name, &engine, &kind); err != nil {
			break
		}
		if !identifier.MatchString(name) || engine != "InnoDB" || kind != "BASE TABLE" {
			err = errors.New("only regular InnoDB tables are supported")
			break
		}
		names = append(names, name)
	}
	err = errors.Join(err, rows.Err(), rows.Close())
	if err != nil || len(names) == 0 {
		return s, errors.Join(err, errors.New("table inventory incomplete"))
	}
	for _, name := range names {
		t := Table{Name: name}
		var ignored, ddl string
		if err = conn.QueryRowContext(ctx, "SHOW CREATE TABLE `"+name+"`").Scan(&ignored, &ddl); err != nil {
			return s, err
		}
		t.DDL = digest([]byte(ddl))
		columns, err := conn.QueryContext(ctx, "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION", database, name)
		if err != nil {
			return s, err
		}
		var expressions []string
		for columns.Next() {
			var column string
			if err = columns.Scan(&column); err != nil {
				break
			}
			if !identifier.MatchString(column) {
				err = errors.New("unsafe column identifier")
				break
			}
			expressions = append(expressions, "HEX(CAST(`"+column+"` AS BINARY))")
		}
		err = errors.Join(err, columns.Err(), columns.Close())
		if err != nil || len(expressions) == 0 {
			return s, errors.Join(err, errors.New("column inventory incomplete"))
		}
		query := "SELECT SHA2(CAST(JSON_ARRAY(" + strings.Join(expressions, ",") + ") AS CHAR CHARACTER SET utf8mb4),256) AS row_hash FROM `" + name + "` ORDER BY row_hash"
		values, err := conn.QueryContext(ctx, query)
		if err != nil {
			return s, err
		}
		hash := sha256.New()
		for values.Next() {
			var row string
			if err = values.Scan(&row); err != nil {
				break
			}
			if len(row) != 64 {
				err = errors.New("invalid row digest")
				break
			}
			io.WriteString(hash, row+"\n")
			t.Rows++
		}
		err = errors.Join(err, values.Err(), values.Close())
		if err != nil {
			return s, err
		}
		t.Digest = hex.EncodeToString(hash.Sum(nil))
		s.Tables = append(s.Tables, t)
	}
	if s.Data, err = Files(ctx, data, true); err != nil {
		return s, err
	}
	if s.Secrets, err = Files(ctx, secrets, false); err != nil {
		return s, err
	}
	return s, nil
}

// Files refuses symlinks, special files and in-flight legacy/native journals.
// Only exact local admission/maintenance metadata is excluded from comparison.
func Files(ctx context.Context, directory string, data bool) ([]File, error) {
	info, err := os.Lstat(directory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("regular inventory root required")
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	opened, err := root.Stat(".")
	if err != nil || !os.SameFile(info, opened) {
		return nil, errors.New("inventory root changed")
	}
	var files []File
	excluded := map[string]bool{".native-store": true, ".native-store.lock": true, ".native-runtime": true, ".native-runtime.lock": true, ".native-admission.lock": true, ".native-maintenance-control.lock": true, ".frontiercloud-native-maintenance": true}
	err = fs.WalkDir(root.FS(), ".", func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return errors.New("symlink in migration inventory")
		}
		if data && excluded[path] {
			if !entry.Type().IsRegular() {
				return errors.New("unsafe migration control metadata")
			}
			return nil
		}
		if data && (strings.HasPrefix(filepath.Base(path), ".upload-") || strings.HasPrefix(filepath.Base(path), ".delete-") || strings.HasPrefix(filepath.Base(path), ".rename-") || filepath.Base(path) == ".recovery-required") {
			return errors.New("pending physical journal requires explicit recovery")
		}
		before, err := root.Lstat(path)
		if err != nil {
			return err
		}
		if before.IsDir() {
			files = append(files, File{path, 0, uint32(before.Mode()), digest(nil)})
			return nil
		}
		if !before.Mode().IsRegular() {
			return errors.New("special file in migration inventory")
		}
		file, err := root.Open(path)
		if err != nil {
			return err
		}
		defer file.Close()
		opened, err := file.Stat()
		if err != nil || !os.SameFile(before, opened) {
			return errors.New("inventory file changed")
		}
		hash := sha256.New()
		buffer := make([]byte, 1024*1024)
		var count int64
		for {
			if err := ctx.Err(); err != nil {
				return err
			}
			n, readErr := file.Read(buffer)
			if n > 0 {
				hash.Write(buffer[:n])
				count += int64(n)
			}
			if errors.Is(readErr, io.EOF) {
				break
			}
			if readErr != nil {
				return readErr
			}
		}
		after, err := root.Lstat(path)
		if err != nil || !os.SameFile(before, after) || before.Size() != count || after.Size() != count || before.Mode() != after.Mode() || !before.ModTime().Equal(after.ModTime()) {
			return errors.New("inventory file mutated during hash")
		}
		files = append(files, File{path, count, uint32(before.Mode().Perm()), hex.EncodeToString(hash.Sum(nil))})
		return nil
	})
	return files, err
}

func digest(raw []byte) string { sum := sha256.Sum256(raw); return hex.EncodeToString(sum[:]) }

func Decode(raw []byte, expectedSHA string) (Snapshot, error) {
	var s Snapshot
	if len(expectedSHA) != 64 || digest(raw) != expectedSHA {
		return s, errors.New("recovery proof SHA256 mismatch")
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&s); err != nil {
		return s, err
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return s, errors.New("trailing recovery proof data")
	}
	if s.Format != Format || !nodeIdentifier.MatchString(s.NodeID) || len(s.Tables) == 0 || len(s.Data) == 0 || len(s.Secrets) == 0 {
		return s, errors.New("incomplete recovery proof")
	}
	return s, nil
}

func Compare(source, recovered Snapshot) error {
	if !reflect.DeepEqual(source, recovered) {
		return fmt.Errorf("stopped source does not exactly match restored recovery inventory; no admission published")
	}
	return nil
}
