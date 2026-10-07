package main

// Explicit OFFLINE second-stage migration. This creates a distinct data root,
// never repins the admitted source or opens public traffic. Media are hardlinked
// on the same filesystem, NOT a backup: a separately restored recovery proof is
// mandatory. After public SQLite writes, reverting to frozen MySQL is NOT safe.
import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/migrationproof"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	"github.com/wongyiuming/FrontierCloud/migrations"
)

type sqliteTransferTable struct {
	Name    string   `json:"name"`
	Columns []string `json:"columns"`
	Rows    int64    `json:"rows"`
	Digest  string   `json:"rows_sha256"`
	NextID  int64    `json:"next_id,omitempty"`
}

var tablePattern = regexp.MustCompile("(?i)^CREATE TABLE(?: IF NOT EXISTS)?\\s+`?(\\w+)")

func masterToSQLiteCommand(arguments []string, output io.Writer) error {
	return mysqlToSQLiteCommand(arguments, output, "Master")
}

func followerToSQLiteCommand(arguments []string, output io.Writer) error {
	return mysqlToSQLiteCommand(arguments, output, "Follower")
}

func mysqlToSQLiteCommand(arguments []string, output io.Writer, expectedRole string) error {
	f := flag.NewFlagSet("master-to-sqlite", flag.ContinueOnError)
	manifest := f.String("manifest", "", "independently restored same-MySQL recovery inventory")
	proofSHA := f.String("proof-sha256", "", "exact SHA256 of that inventory")
	nodeID := f.String("node-id", "", "original native Master ID")
	socket := f.String("mysql-socket", "", "local root MySQL socket")
	root := f.String("target-data-root", "", "existing EMPTY distinct target directory")
	linkRoot := f.String("link-target-root", "", "same target directory through the source's single bind mount")
	path := f.String("target-sqlite-path", "", "absent SQLite path INSIDE target root; use final container path")
	report := f.String("report", "", "new private report outside both data roots")
	recoveryPath := f.String("sqlite-recovery-file", "", "new independent SQLite recovery file in a private outside-root directory")
	if err := f.Parse(arguments); err != nil {
		return err
	}
	if f.NArg() != 0 || !node.ValidIdentifier(*nodeID) || len(*proofSHA) != 64 {
		return errors.New("exact node-id and restored proof SHA256 required")
	}
	for _, p := range []string{*manifest, *socket, *root, *linkRoot, *path, *report, *recoveryPath} {
		if !filepath.IsAbs(p) || filepath.Clean(p) != p {
			return errors.New("clean absolute paths required")
		}
	}
	c, err := config.Load()
	if err != nil {
		return err
	}
	if c.DatabaseType != config.DatabaseMySQL {
		return errors.New("source must be the admitted native MySQL Master")
	}
	if within(c.DataRoot, *root) || within(*root, c.DataRoot) || within(c.SecretsDirectory, *root) || within(*root, c.SecretsDirectory) || within(c.DataRoot, *linkRoot) || within(*linkRoot, c.DataRoot) || within(c.SecretsDirectory, *linkRoot) || within(*linkRoot, c.SecretsDirectory) || !within(*root, *path) || *path == *root {
		return errors.New("distinct nonoverlapping data roots and an inside-root SQLite path required")
	}
	for _, p := range []string{*manifest, *report, *recoveryPath} {
		for _, directory := range []string{c.DataRoot, c.SecretsDirectory, *root, *linkRoot} {
			if within(directory, p) {
				return errors.New("proof/report must be outside all inventoried roots")
			}
		}
	}
	info, err := os.Lstat(*manifest)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || info.Size() > 16*1024*1024 {
		return errors.New("private regular restored inventory required")
	}
	raw, err := os.ReadFile(*manifest)
	if err != nil {
		return err
	}
	recovery, err := migrationproof.Decode(raw, *proofSHA)
	if err != nil {
		return err
	}
	expectedFormat := migrationproof.Format
	if expectedRole == "Follower" {
		expectedFormat = migrationproof.FollowerFormat
	}
	if recovery.Format != expectedFormat {
		return errors.New("restored proof role differs from migration target")
	}
	if _, err = os.Lstat(*report); !errors.Is(err, os.ErrNotExist) {
		return errors.New("report must not exist")
	}
	if _, err = os.Lstat(*recoveryPath); !errors.Is(err, os.ErrNotExist) {
		return errors.New("SQLite recovery file must not exist")
	}
	parent, err := os.Lstat(filepath.Dir(*recoveryPath))
	if err != nil || !parent.IsDir() || parent.Mode().Perm()&0077 != 0 {
		return errors.New("SQLite recovery directory must be private")
	}
	info, err = os.Lstat(*root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("existing regular target directory required")
	}
	alias, err := os.Lstat(*linkRoot)
	if err != nil || !alias.IsDir() || alias.Mode()&os.ModeSymlink != 0 || !os.SameFile(info, alias) {
		return errors.New("link target alias must be the exact same regular target directory")
	}
	entries, err := os.ReadDir(*root)
	if err != nil || len(entries) != 0 {
		return errors.New("target directory must be completely empty")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	gate, err := maintenance.Open(c.DataRoot)
	if err != nil {
		return err
	}
	defer gate.Close()
	return gate.Inspect(ctx, func(ctx context.Context) error {
		if err := bindStore(ctx, c); err != nil {
			return err
		}
		db, err := openExistingStore(ctx, c)
		if err != nil {
			return err
		}
		defer db.Close()
		identity, err := node.OpenExisting(ctx, db.Nodes(), c.SecretsDirectory)
		if err != nil {
			return err
		}
		if identity.ID != *nodeID || identity.Role != expectedRole {
			return errors.New("source " + expectedRole + " identity mismatch")
		}
		if expectedRole == "Master" {
			if err = identity.CheckNativeRuntime(ctx, c.DataRoot); err != nil {
				return err
			}
		} else if expectedRole != "Follower" {
			return errors.New("unsupported offline migration role")
		}
		if recovery.NodeID != identity.ID {
			return errors.New("restored proof identity differs from source")
		}
		conn, release, err := lockMigrationMySQL(ctx, c, *socket)
		if err != nil {
			return err
		}
		defer release()
		var rootUUID, runtimeUUID string
		if err = conn.QueryRowContext(ctx, "SELECT @@server_uuid").Scan(&rootUUID); err != nil {
			return err
		}
		if err = db.(interface{ Database() *sql.DB }).Database().QueryRowContext(ctx, "SELECT @@server_uuid").Scan(&runtimeUUID); err != nil || rootUUID != runtimeUUID {
			return errors.New("runtime store differs from fenced MySQL")
		}
		if err = validateMigrationState(ctx, conn, c.SecretsDirectory); err != nil {
			return err
		}
		capture := migrationproof.Capture
		if expectedRole == "Follower" {
			capture = migrationproof.CaptureFollower
		}
		source, err := capture(ctx, conn, c.MySQLDatabase, c.DataRoot, c.SecretsDirectory)
		if err != nil {
			return err
		}
		if err = migrationproof.Compare(source, recovery); err != nil {
			return err
		}
		if err = validateMigrationFiles(ctx, conn, source); err != nil {
			return err
		}
		if err = linkMigrationData(ctx, c.DataRoot, *linkRoot, source.Data); err != nil {
			return err
		}
		targetConfig := c
		targetConfig.DatabaseType = config.DatabaseSQLite
		targetConfig.DataRoot = *root
		targetConfig.SQLitePath = *path
		targetGate, err := maintenance.Open(*root)
		if err != nil {
			return err
		}
		defer targetGate.Close()
		if err = targetGate.Enter(ctx, func(context.Context) error { return nil }); err != nil {
			return err
		}
		target, err := sqlitestore.Open(*path)
		if err != nil {
			return err
		}
		tables, err := transferMySQLRows(ctx, conn, target.Database())
		if err == nil {
			private, createErr := os.OpenFile(*recoveryPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
			if createErr != nil {
				err = createErr
			} else {
				err = private.Close()
			}
			if err == nil {
				_, err = target.Database().ExecContext(ctx, "VACUUM INTO ?", *recoveryPath)
			}
		}
		closeErr := target.Close()
		if err != nil || closeErr != nil {
			return errors.Join(err, closeErr)
		}
		// Driver-created DB/WAL files inherit the target root owner, not operator root.
		openedRoot, err := os.OpenRoot(*root)
		if err != nil {
			return err
		}
		defer openedRoot.Close()
		dbFile, err := os.OpenFile(*path, os.O_RDWR, 0600)
		if err != nil {
			return err
		}
		err = fsutil.InheritOwner(dbFile, openedRoot)
		if err == nil {
			err = dbFile.Sync()
		}
		err = errors.Join(err, dbFile.Close())
		if err != nil {
			return err
		}
		readOnly, err := sqlitestore.OpenReadOnly(ctx, *path)
		if err != nil {
			return err
		}
		defer readOnly.Close()
		recoveredSQLite, err := sqlitestore.OpenReadOnly(ctx, *recoveryPath)
		if err != nil {
			return err
		}
		defer recoveredSQLite.Close()
		targetIdentity, err := node.OpenExisting(ctx, readOnly.Nodes(), c.SecretsDirectory)
		if err != nil {
			return err
		}
		if targetIdentity.NodeIdentity != identity.NodeIdentity {
			return errors.New("target identity changed")
		}
		verify := func(ctx context.Context) (func(), error) {
			actual, err := sqliteTransferInventory(ctx, readOnly.Database(), tables)
			if err != nil {
				return nil, err
			}
			if !reflect.DeepEqual(tables, actual) {
				return nil, errors.New("SQLite full row/NULL/generated-column/next-ID proof mismatch")
			}
			restored, err := sqliteTransferInventory(ctx, recoveredSQLite.Database(), tables)
			if err != nil {
				return nil, err
			}
			if !reflect.DeepEqual(tables, restored) {
				return nil, errors.New("independent SQLite recovery row proof mismatch")
			}
			files, err := migrationproof.Files(ctx, *root, true)
			if err != nil {
				return nil, err
			}
			var businessFiles []migrationproof.File
			rel, _ := filepath.Rel(*root, *path)
			for _, file := range files {
				if file.Path != rel && file.Path != rel+"-wal" && file.Path != rel+"-shm" {
					businessFiles = append(businessFiles, file)
				}
			}
			if !reflect.DeepEqual(source.Data, businessFiles) {
				return nil, errors.New("target business file/permission proof mismatch")
			}
			secrets, err := migrationproof.Files(ctx, c.SecretsDirectory, false)
			if err != nil {
				return nil, err
			}
			if !reflect.DeepEqual(source.Secrets, secrets) {
				return nil, errors.New("secrets changed during transfer")
			}
			if err = bindStore(ctx, targetConfig); err != nil {
				return nil, err
			}
			// The source maintenance lease and FTWRL remain held by the OUTER scope
			// through admission and durable report, even after this callback returns.
			return func() {}, nil
		}
		admit := targetIdentity.AdmitVerifiedMaster
		if expectedRole == "Follower" {
			admit = targetIdentity.AdmitVerifiedFollower
		}
		if err = admit(ctx, *root, verify); err != nil {
			return err
		}
		if err = targetIdentity.CheckNativeRuntime(ctx, *root); err != nil {
			return err
		}
		recoveryFile, err := os.OpenFile(*recoveryPath, os.O_RDWR, 0600)
		if err != nil {
			return err
		}
		err = errors.Join(recoveryFile.Sync(), recoveryFile.Close())
		if err != nil {
			return err
		}
		result := map[string]any{"format": "frontiercloud-verified-mysql-to-sqlite-v1", "node_id": *nodeID, "recovery_sha256": *proofSHA, "tables": tables, "files": len(source.Data), "secrets": len(source.Secrets), "target_admitted": true, "maintenance_enabled": true, "public_opened": false, "hardlinks_are_not_backup": true, "frozen_mysql_rollback_after_public_writes_safe": false}
		bytes, err := json.MarshalIndent(result, "", "  ")
		if err != nil {
			return err
		}
		bytes = append(bytes, '\n')
		if err = writeSQLiteTransferReport(*report, bytes); err != nil {
			return err
		}
		return json.NewEncoder(output).Encode(map[string]any{"node_id": *nodeID, "tables": len(tables), "files": len(source.Data), "target_admitted": true, "maintenance_enabled": true, "report_sha256": sqliteDigest(bytes)})
	})
}

func within(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}

func linkMigrationData(ctx context.Context, source, target string, files []migrationproof.File) error {
	for _, file := range files {
		if err := ctx.Err(); err != nil {
			return err
		}
		from, to := filepath.Join(source, file.Path), filepath.Join(target, file.Path)
		info, err := os.Lstat(from)
		if err != nil {
			return err
		}
		if info.IsDir() {
			if file.Path != "." {
				if err = os.Mkdir(to, info.Mode().Perm()); err != nil {
					return err
				}
			}
			opened, err := os.Open(to)
			if err != nil {
				return err
			}
			parent, err := os.OpenRoot(from)
			if err != nil {
				opened.Close()
				return err
			}
			err = fsutil.InheritOwner(opened, parent)
			parent.Close()
			if err == nil {
				err = opened.Chmod(info.Mode().Perm())
			}
			err = errors.Join(err, opened.Close())
			if err != nil {
				return err
			}
		} else {
			if !info.Mode().IsRegular() {
				return errors.New("unsafe source file")
			}
			if err = os.Link(from, to); err != nil {
				return fmt.Errorf("same-filesystem business file linking failed: %w", err)
			}
		}
	}
	// Persist every newly created directory entry (including hardlinks), not
	// merely the final admission/report in the top-level root.
	for i := len(files) - 1; i >= 0; i-- {
		if os.FileMode(files[i].Mode).IsDir() {
			directory, err := os.Open(filepath.Join(target, files[i].Path))
			if err != nil {
				return err
			}
			if err = errors.Join(directory.Sync(), directory.Close()); err != nil {
				return err
			}
		}
	}
	return nil
}

func sqliteDigest(raw []byte) string { sum := sha256.Sum256(raw); return hex.EncodeToString(sum[:]) }

func writeSQLiteTransferReport(path string, raw []byte) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, err = f.Write(raw)
	if err == nil {
		err = f.Sync()
	}
	err = errors.Join(err, f.Close())
	if err != nil {
		return err
	}
	parent, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	return errors.Join(parent.Sync(), parent.Close())
}

type transferColumn struct {
	name            string
	generated, auto bool
}

func transferMySQLRows(ctx context.Context, source *sql.Conn, target *sql.DB) ([]sqliteTransferTable, error) {
	// Create the canonical SQLite schema, never translate a SQL dump textually.
	statements, err := migrations.Statements("sqlite")
	if err != nil {
		return nil, err
	}
	names := map[string]bool{}
	for _, statement := range statements {
		if match := tablePattern.FindStringSubmatch(statement); len(match) == 2 {
			names[match[1]] = true
		}
	}
	rows, err := source.QueryContext(ctx, "SELECT TABLE_NAME,COALESCE(AUTO_INCREMENT,0) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME")
	if err != nil {
		return nil, err
	}
	var tables []sqliteTransferTable
	for rows.Next() {
		var t sqliteTransferTable
		if err = rows.Scan(&t.Name, &t.NextID); err != nil {
			break
		}
		if !names[t.Name] {
			err = errors.New("unmapped source table")
			break
		}
		tables = append(tables, t)
	}
	err = errors.Join(err, rows.Err(), rows.Close())
	if err != nil {
		return nil, err
	}
	if len(tables) != len(names) {
		return nil, errors.New("source/target table sets differ")
	}
	columns := map[string][]transferColumn{}
	autoTables := map[string]string{}
	for _, t := range tables {
		r, err := source.QueryContext(ctx, "SELECT COLUMN_NAME,EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? ORDER BY ORDINAL_POSITION", t.Name)
		if err != nil {
			return nil, err
		}
		for r.Next() {
			var col transferColumn
			var extra string
			if err = r.Scan(&col.name, &extra); err != nil {
				break
			}
			if !safeTransferIdentifier(col.name) {
				err = errors.New("unsafe column")
				break
			}
			col.generated = strings.Contains(extra, "GENERATED") && !strings.Contains(extra, "DEFAULT_GENERATED")
			col.auto = strings.Contains(extra, "auto_increment")
			if col.auto {
				if (col.name != "id" && !(t.Name == "node_identity" && col.name == "singleton")) || t.NextID < 1 {
					err = errors.New("unsupported auto increment")
					break
				}
				autoTables[t.Name] = col.name
			}
			columns[t.Name] = append(columns[t.Name], col)
		}
		err = errors.Join(err, r.Err(), r.Close())
		if err != nil {
			return nil, err
		}
	}
	for _, statement := range statements {
		if match := tablePattern.FindStringSubmatch(statement); len(match) == 2 && autoTables[match[1]] != "" {
			statement, err = transferAutoDDL(statement, autoTables[match[1]])
			if err != nil {
				return nil, err
			}
		}
		if _, err = target.ExecContext(ctx, statement); err != nil {
			return nil, err
		}
	}
	tx, err := target.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	for i := range tables {
		t := &tables[i]
		var all, insert []string
		var indices []int
		for index, col := range columns[t.Name] {
			all = append(all, "`"+col.name+"`")
			t.Columns = append(t.Columns, col.name)
			if !col.generated {
				insert = append(insert, "`"+col.name+"`")
				indices = append(indices, index)
			}
		}
		// table_xinfo includes generated values. Name/order and writable status
		// must match; silently dropping unknown or generated source columns fails.
		r, err := tx.QueryContext(ctx, "PRAGMA table_xinfo(`"+t.Name+"`)")
		if err != nil {
			return nil, err
		}
		index := 0
		for r.Next() {
			var cid, notnull, pk, hidden int
			var name, kind string
			var def any
			if err = r.Scan(&cid, &name, &kind, &notnull, &def, &pk, &hidden); err != nil {
				break
			}
			if index >= len(columns[t.Name]) || name != columns[t.Name][index].name || (hidden > 1) != columns[t.Name][index].generated {
				err = errors.New("source/target column mapping differs")
				break
			}
			index++
		}
		err = errors.Join(err, r.Err(), r.Close())
		if err != nil {
			return nil, err
		}
		if index != len(columns[t.Name]) {
			return nil, errors.New("incomplete target column mapping")
		}
		query := "INSERT INTO `" + t.Name + "` (" + strings.Join(insert, ",") + ") VALUES (" + strings.TrimSuffix(strings.Repeat("?,", len(insert)), ",") + ")"
		statement, err := tx.PrepareContext(ctx, query)
		if err != nil {
			return nil, err
		}
		values, err := source.QueryContext(ctx, "SELECT "+strings.Join(all, ",")+" FROM `"+t.Name+"`")
		if err != nil {
			statement.Close()
			return nil, err
		}
		for values.Next() {
			raw := make([]any, len(all))
			scan := make([]any, len(all))
			for j := range raw {
				scan[j] = &raw[j]
			}
			if err = values.Scan(scan...); err != nil {
				break
			}
			args := make([]any, len(indices))
			for j, k := range indices {
				args[j] = raw[k]
				if b, ok := args[j].([]byte); ok && !(t.Name == "cluster_business_backup_chunks" && columns[t.Name][k].name == "payload") {
					args[j] = string(b)
				}
			}
			if _, err = statement.ExecContext(ctx, args...); err != nil {
				break
			}
		}
		err = errors.Join(err, values.Err(), values.Close(), statement.Close())
		if err != nil {
			return nil, fmt.Errorf("copy table %s: %w", t.Name, err)
		}
		if autoTables[t.Name] != "" {
			if _, err = tx.ExecContext(ctx, "DELETE FROM sqlite_sequence WHERE name=?", t.Name); err != nil {
				return nil, err
			}
			if _, err = tx.ExecContext(ctx, "INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", t.Name, t.NextID-1); err != nil {
				return nil, err
			}
		}
		t.Rows, t.Digest, err = transferRowDigest(ctx, source, "mysql", *t)
		if err != nil {
			return nil, err
		}
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	var result string
	if err = target.QueryRowContext(ctx, "PRAGMA integrity_check").Scan(&result); err != nil || result != "ok" {
		return nil, errors.New("SQLite integrity_check failed")
	}
	return tables, nil
}

func transferAutoDDL(statement, column string) (string, error) {
	inline := column + " INTEGER NOT NULL PRIMARY KEY,"
	if strings.Count(statement, inline) == 1 {
		return strings.Replace(statement, inline, column+" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,", 1), nil
	}
	definition := column + " INTEGER NOT NULL,"
	constraint := "PRIMARY KEY (" + column + ")"
	if strings.Count(statement, definition) != 1 || strings.Count(statement, constraint) != 1 {
		return "", errors.New("unsupported target auto-ID definition")
	}
	statement = strings.Replace(statement, definition, column+" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,", 1)
	// The preceding comma belongs to the last column when a table-level primary
	// key is removed. Keep every other canonical constraint/index unchanged.
	position := strings.Index(statement, constraint)
	before := strings.TrimRight(statement[:position], " \r\n\t")
	if !strings.HasSuffix(before, ",") {
		return "", errors.New("unsupported table-level primary key")
	}
	return strings.TrimSuffix(before, ",") + statement[position+len(constraint):], nil
}

func safeTransferIdentifier(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_') {
			return false
		}
	}
	return true
}

type transferQuerier interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func transferRowDigest(ctx context.Context, db transferQuerier, backend string, t sqliteTransferTable) (int64, string, error) {
	var expressions []string
	cast := "BLOB"
	if backend == "mysql" {
		cast = "BINARY"
	}
	for _, name := range t.Columns {
		if !safeTransferIdentifier(name) {
			return 0, "", errors.New("unsafe column")
		}
		expressions = append(expressions, "CASE WHEN `"+name+"` IS NULL THEN NULL ELSE HEX(CAST(`"+name+"` AS "+cast+")) END")
	}
	rows, err := db.QueryContext(ctx, "SELECT "+strings.Join(expressions, ",")+" FROM `"+t.Name+"`")
	if err != nil {
		return 0, "", err
	}
	defer rows.Close()
	var hashes []string
	for rows.Next() {
		values := make([]sql.NullString, len(expressions))
		scan := make([]any, len(values))
		for i := range values {
			scan[i] = &values[i]
		}
		if err = rows.Scan(scan...); err != nil {
			return 0, "", err
		}
		canonical := make([]any, len(values))
		for i, v := range values {
			if v.Valid {
				canonical[i] = v.String
			}
		}
		raw, err := json.Marshal(canonical)
		if err != nil {
			return 0, "", err
		}
		hashes = append(hashes, sqliteDigest(raw))
	}
	if err = rows.Err(); err != nil {
		return 0, "", err
	}
	sort.Strings(hashes)
	return int64(len(hashes)), sqliteDigest([]byte(strings.Join(hashes, "\n"))), nil
}

func sqliteTransferInventory(ctx context.Context, db *sql.DB, expected []sqliteTransferTable) ([]sqliteTransferTable, error) {
	actual := make([]sqliteTransferTable, len(expected))
	for i, t := range expected {
		actual[i] = t
		var err error
		actual[i].Rows, actual[i].Digest, err = transferRowDigest(ctx, db, "sqlite", t)
		if err != nil {
			return nil, err
		}
		if t.NextID > 0 {
			var seq int64
			if err = db.QueryRowContext(ctx, "SELECT seq FROM sqlite_sequence WHERE name=?", t.Name).Scan(&seq); err != nil {
				return nil, err
			}
			actual[i].NextID = seq + 1
		}
	}
	return actual, nil
}
