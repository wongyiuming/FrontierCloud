package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	driver "github.com/go-sql-driver/mysql"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/migrationproof"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/vault"
)

// This is deliberately NOT a general restore or an automatic release path.
// Stop legacy Web/Updater and filesystem writers first; restore a backup on an
// egress-isolated fixture, capture it, and compare it with the stopped source.
// MySQL's global read lock additionally fences unknown database writers until
// durable admission. Existing data, keys, relationships and intents are not
// modified. The maintenance marker remains closed until explicit resume.
func masterMigrationCommand(arguments []string, output io.Writer) error {
	return offlineMigrationCommand(arguments, output, "Master")
}

// Followers get snapshot proof only: conversion publishes admission in the
// separate SQLite target, never in the original legacy MySQL store.
func followerMigrationCommand(arguments []string, output io.Writer) error {
	if len(arguments) == 0 || arguments[0] != "snapshot" {
		return errors.New("follower-migration requires snapshot; use follower-to-sqlite for verified target admission")
	}
	return offlineMigrationCommand(arguments, output, "Follower")
}

func offlineMigrationCommand(arguments []string, output io.Writer, expectedRole string) error {
	if len(arguments) == 0 || (arguments[0] != "snapshot" && arguments[0] != "admit") {
		return errors.New("master-migration requires snapshot or admit")
	}
	action := arguments[0]
	flags := flag.NewFlagSet("master-migration "+action, flag.ContinueOnError)
	manifest := flags.String("manifest", "", "private restored inventory outside DATA_ROOT and SECRETS_DIR")
	proofSHA := flags.String("proof-sha256", "", "exact restored inventory SHA256, mandatory for admit")
	expectedID := flags.String("node-id", "", "exact existing Master ID")
	socket := flags.String("mysql-socket", "", "local authoritative MySQL socket (root password stays in its secret file)")
	if err := flags.Parse(arguments[1:]); err != nil {
		return err
	}
	if flags.NArg() != 0 || !node.ValidIdentifier(*expectedID) || !filepath.IsAbs(*manifest) || !filepath.IsAbs(*socket) || (action == "admit" && len(*proofSHA) != 64) {
		return errors.New("exact node-id, absolute manifest and mysql-socket required; admit also requires proof-sha256")
	}
	c, err := config.Load()
	if err != nil {
		return err
	}
	if c.DatabaseType != config.DatabaseMySQL {
		return errors.New("first-stage Master migration must retain MySQL")
	}
	for _, directory := range []string{c.DataRoot, c.SecretsDirectory} {
		rel, err := filepath.Rel(directory, *manifest)
		if err != nil || (rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))) {
			return errors.New("manifest must be outside inventoried roots")
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	gate, err := maintenance.Open(c.DataRoot)
	if err != nil {
		return err
	}
	defer gate.Close()
	// Fencing is safe even on failure: never reopen or invent identity implicitly.
	if err = gate.Enter(ctx, func(context.Context) error { return nil }); err != nil {
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
	if identity.Role != expectedRole || identity.ID != *expectedID {
		return errors.New("existing " + expectedRole + " identity mismatch")
	}
	var recovered migrationproof.Snapshot
	if action == "admit" {
		info, err := os.Lstat(*manifest)
		if err != nil || !info.Mode().IsRegular() || info.Size() > 16*1024*1024 || info.Mode().Perm()&0077 != 0 {
			return errors.New("private regular recovery proof required")
		}
		raw, err := os.ReadFile(*manifest)
		if err != nil {
			return err
		}
		recovered, err = migrationproof.Decode(raw, *proofSHA)
		if err != nil {
			return err
		}
	}
	var snapshot migrationproof.Snapshot
	verify := func(ctx context.Context) (func(), error) {
		conn, release, err := lockMigrationMySQL(ctx, c, *socket)
		if err != nil {
			return nil, err
		}
		fail := func(err error) (func(), error) { release(); return nil, err }
		current, err := db.Nodes().ReadIdentity(ctx)
		if err != nil || current != identity.NodeIdentity {
			return fail(errors.New("identity changed before authoritative fence"))
		}
		// The root socket and ordinary runtime connection must refer to one server.
		var lockedUUID, runtimeUUID string
		if err = conn.QueryRowContext(ctx, "SELECT @@server_uuid").Scan(&lockedUUID); err != nil {
			return fail(err)
		}
		runtimeDB, ok := db.(interface{ Database() *sql.DB })
		if !ok {
			return fail(errors.New("MySQL infrastructure connection required"))
		}
		if err = runtimeDB.Database().QueryRowContext(ctx, "SELECT @@server_uuid").Scan(&runtimeUUID); err != nil || runtimeUUID != lockedUUID {
			return fail(errors.New("runtime store and write-fenced server differ"))
		}
		if err = validateMigrationState(ctx, conn, c.SecretsDirectory); err != nil {
			return fail(err)
		}
		capture := migrationproof.Capture
		if expectedRole == "Follower" {
			capture = migrationproof.CaptureFollower
		}
		snapshot, err = capture(ctx, conn, c.MySQLDatabase, c.DataRoot, c.SecretsDirectory)
		if err != nil {
			return fail(err)
		}
		if snapshot.NodeID != *expectedID {
			return fail(errors.New("snapshot identity mismatch"))
		}
		if err = validateMigrationFiles(ctx, conn, snapshot); err != nil {
			return fail(err)
		}
		if action == "admit" {
			if err = migrationproof.Compare(snapshot, recovered); err != nil {
				return fail(err)
			}
			// Bind only after proof; it validates the existing decrypted identity.
			if err = bindStore(ctx, c); err != nil {
				return fail(err)
			}
		}
		return release, nil
	}
	if action == "snapshot" {
		err = gate.Inspect(ctx, func(ctx context.Context) error {
			release, err := verify(ctx)
			if err != nil {
				return err
			}
			defer release()
			raw, err := json.MarshalIndent(snapshot, "", "  ")
			if err != nil {
				return err
			}
			raw = append(raw, '\n')
			f, err := os.OpenFile(*manifest, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
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
			parent, err := os.Open(filepath.Dir(*manifest))
			if err != nil {
				return err
			}
			err = errors.Join(parent.Sync(), parent.Close())
			if err != nil {
				return err
			}
			sum := sha256.Sum256(raw)
			return json.NewEncoder(output).Encode(map[string]any{"snapshot_sha256": hex.EncodeToString(sum[:]), "node_id": snapshot.NodeID, "tables": len(snapshot.Tables), "files": len(snapshot.Data), "secrets": len(snapshot.Secrets), "maintenance_enabled": true})
		})
	} else {
		err = identity.AdmitVerifiedMaster(ctx, c.DataRoot, verify)
		if err == nil {
			err = json.NewEncoder(output).Encode(map[string]any{"admitted": true, "node_id": identity.ID, "maintenance_enabled": true})
		}
	}
	return err
}

func lockMigrationMySQL(ctx context.Context, c config.Config, socket string) (*sql.Conn, func(), error) {
	password, err := os.ReadFile(filepath.Join(c.SecretsDirectory, "mysql_root_password"))
	if err != nil {
		return nil, nil, err
	}
	dsn := driver.NewConfig()
	dsn.User, dsn.Passwd, dsn.Net, dsn.Addr, dsn.DBName = "root", strings.TrimSpace(string(password)), "unix", socket, c.MySQLDatabase
	dsn.Timeout, dsn.ReadTimeout, dsn.WriteTimeout = 10*time.Second, 120*time.Second, 10*time.Second
	db, err := sql.Open("mysql", dsn.FormatDSN())
	if err != nil {
		return nil, nil, err
	}
	conn, err := db.Conn(ctx)
	if err != nil {
		db.Close()
		return nil, nil, err
	}
	release := func() {
		unlockCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		conn.ExecContext(unlockCtx, "UNLOCK TABLES")
		conn.Close()
		db.Close()
	}
	for _, statement := range migrationMySQLLockPlan() {
		if _, err = conn.ExecContext(ctx, statement); err != nil {
			release()
			return nil, nil, err
		}
	}
	return conn, release, nil
}

func migrationMySQLLockPlan() []string {
	// MySQL's default statistics cache can report NULL/0 AUTO_INCREMENT on a
	// never-written table although its actual next value is 1. Migration must
	// read engine metadata, not cache, and must never reset/invent an ID floor.
	// This affects only our locked reader session, not server/global settings.
	return []string{"SET SESSION time_zone='+00:00'", "SET SESSION information_schema_stats_expiry=0", "FLUSH TABLES WITH READ LOCK"}
}

func validateMigrationState(ctx context.Context, conn *sql.Conn, secrets string) error {
	// No repair or cancellation: only expired, unstarted reservations on revoked
	// remote relationships may carry as durable history. Every other unfinished
	// operation requires a separately validated recovery before migration.
	for _, query := range []string{
		"SELECT COUNT(*) FROM media_delete_operations",
		"SELECT COUNT(*) FROM global_media_objects WHERE state<>'active'",
		"SELECT COUNT(*) FROM karaoke_recordings WHERE state NOT IN ('ready','deleted')",
		"SELECT COUNT(*) FROM karaoke_users WHERE status NOT IN ('active','banned','deleted')",
		"SELECT COUNT(*) FROM cluster_business_backups WHERE state NOT IN ('ready','failed')",
		"SELECT COUNT(*) FROM cluster_worker_jobs WHERE state NOT IN ('queued','complete')",
		"SELECT COUNT(*) FROM cluster_upload_sessions u LEFT JOIN cluster_storage_members m ON m.member_id=u.storage_member_id LEFT JOIN node_relationships r ON r.relationship_id=m.relationship_id WHERE u.state NOT IN ('complete','cancelled') AND NOT (u.state='reserved' AND u.expires_at<UNIX_TIMESTAMP() AND COALESCE(r.state,'')='revoked' AND u.storage_member_id<>(SELECT node_id FROM node_identity WHERE singleton=1))",
		"SELECT COUNT(*) FROM cluster_storage_members m WHERE m.reserved_bytes<>COALESCE((SELECT SUM(u.expected_bytes) FROM cluster_upload_sessions u WHERE u.storage_member_id=m.member_id AND u.state NOT IN ('complete','cancelled')),0)",
	} {
		var count int64
		if err := conn.QueryRowContext(ctx, query).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return errors.New("unfinished or inconsistent business intent requires explicit recovery; nothing was discarded")
		}
	}
	v, err := vault.OpenExisting(secrets)
	if err != nil {
		return err
	}
	rows, err := conn.QueryContext(ctx, "SELECT credential FROM node_relationships")
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var encrypted string
		if err = rows.Scan(&encrypted); err != nil {
			return err
		}
		plain, err := v.Unseal(encrypted)
		if err != nil {
			return errors.New("existing relationship cannot be decrypted")
		}
		credential, err := protocol.Decode(plain)
		if err != nil || len(credential) != 48 {
			return errors.New("existing relationship credential invalid")
		}
	}
	return rows.Err()
}

func validateMigrationFiles(ctx context.Context, conn *sql.Conn, snapshot migrationproof.Snapshot) error {
	files := make(map[string]int64)
	for _, file := range snapshot.Data {
		if os.FileMode(file.Mode).IsRegular() {
			files[file.Path] = file.Bytes
		}
	}
	rows, err := conn.QueryContext(ctx, "SELECT media_path,size_bytes FROM global_media_objects WHERE storage_member_id=? AND object_kind<>'directory' AND state='active'", snapshot.NodeID)
	if err != nil {
		return err
	}
	for rows.Next() {
		var path string
		var size int64
		if err = rows.Scan(&path, &size); err != nil {
			break
		}
		actual, exists := files["media/"+path]
		if !fs.ValidPath(path) || !exists || actual != size {
			err = errors.New("active local media is missing or has incorrect size")
			break
		}
	}
	err = errors.Join(err, rows.Err(), rows.Close())
	if err != nil {
		return err
	}
	rows, err = conn.QueryContext(ctx, "SELECT DISTINCT lyric_path FROM media_lyric_links")
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var path string
		if err = rows.Scan(&path); err != nil {
			return err
		}
		if _, exists := files["media/"+path]; !fs.ValidPath(path) || !exists {
			return errors.New("referenced lyric file is missing")
		}
	}
	return rows.Err()
}
