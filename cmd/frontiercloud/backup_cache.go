package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"path/filepath"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/backup"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func cleanupBackupCacheCommand(arguments []string, output io.Writer) error {
	flags := flag.NewFlagSet("cleanup-backup-cache", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	confirmation := flags.String("confirm-node-id", "", "exact current local identity")
	wait := flags.Int("wait-seconds", 30, "bounded offline inspection/cache lease deadline")
	if flags.Parse(arguments) != nil || flags.NArg() != 0 || !node.ValidIdentifier(*confirmation) || *wait < 1 || *wait > 300 {
		return errors.New("cleanup-backup-cache requires confirm-node-id and wait-seconds from 1 through 300")
	}
	c, err := config.Load()
	if err != nil {
		return err
	}
	gate, err := maintenance.Open(c.DataRoot)
	if err != nil {
		return err
	}
	defer gate.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(*wait)*time.Second)
	defer cancel()
	result := map[string]backup.CacheCleanup{}
	err = gate.Inspect(ctx, func(ctx context.Context) error {
		if err := bindStore(ctx, c); err != nil {
			return err
		}
		var db store.Store
		var err error
		if c.DatabaseType == config.DatabaseSQLite {
			db, err = sqlitestore.OpenExisting(ctx, c.SQLitePath)
		} else {
			db, err = openExistingStore(ctx, c)
		}
		if err != nil {
			return err
		}
		defer db.Close()
		if _, err := db.Maintenance().InspectMaintenance(ctx); err != nil {
			return err
		}
		identity, err := node.OpenExisting(ctx, db.Nodes(), c.SecretsDirectory)
		if err != nil || identity.ID != *confirmation {
			return store.ErrNodeState
		}
		audit := store.AdminAudit{SessionHash: "native-offline-operator", Action: "backup_cache_cleanup", SourceSummary: "native-private-caches", Result: "pending", TargetCount: 2}
		if err := db.Admin().AppendAudit(ctx, audit); err != nil {
			return err // Durable intent BEFORE deleting any owned staging file.
		}
		for _, kind := range []struct {
			name    string
			scratch bool
		}{{".business-backups", false}, {".backup-preflight", true}} {
			result[kind.name], err = backup.CleanupCache(ctx, filepath.Join(c.DataRoot, kind.name), kind.scratch)
			if err != nil {
				break
			}
		}
		audit.Result = "success"
		if err != nil {
			audit.Result = "failed"
		}
		// Never leak paths/artifact contents into an error/audit record.
		cleanup, stop := context.WithTimeout(context.WithoutCancel(ctx), 15*time.Second)
		defer stop()
		return errors.Join(err, db.Admin().AppendAudit(cleanup, audit))
	})
	if err != nil {
		return err
	}
	return json.NewEncoder(output).Encode(map[string]any{"caches": result, "maintenance_enabled": true, "scope": "claimed-native-private-cache-files", "restore_ready": false})
}
