package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"path/filepath"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func drainRenameCommand(arguments []string, output io.Writer) error {
	flags := flag.NewFlagSet("drain-rename-history", flag.ContinueOnError)
	confirmation := flags.String("confirm-node-id", "", "exact current local identity")
	wait := flags.Int("wait-seconds", 300, "bounded native offline deadline")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	if flags.NArg() != 0 || !node.ValidIdentifier(*confirmation) || *wait < 1 || *wait > 300 {
		return errors.New("drain-rename-history requires confirm-node-id and wait-seconds from 1 through 300")
	}
	settings, err := config.Load()
	if err != nil {
		return err
	}
	gate, err := maintenance.Open(settings.DataRoot)
	if err != nil {
		return err
	}
	defer gate.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(*wait)*time.Second)
	defer cancel()
	var drained int
	err = gate.Inspect(ctx, func(ctx context.Context) error {
		var db store.Store
		var err error
		if settings.DatabaseType == config.DatabaseSQLite {
			db, err = sqlitestore.OpenExisting(ctx, settings.SQLitePath)
		} else {
			db, err = openExistingStore(ctx, settings)
		}
		if err != nil {
			return err
		}
		defer db.Close()
		snapshot, err := db.Maintenance().InspectMaintenance(ctx)
		if err != nil {
			return err
		}
		if !snapshot.LogicalIdle {
			return store.ErrBackupBusy
		}
		drained, err = media.DrainRenameHistory(ctx, filepath.Join(settings.DataRoot, "media"), db.Maintenance(), db.Media(), *confirmation)
		return err
	})
	if err != nil {
		return err
	}
	return json.NewEncoder(output).Encode(map[string]any{"drained_operations": drained, "native_quiescent": true, "scope": "native-processes-sharing-DATA_ROOT", "maintenance_enabled": true, "python_rollback_ready": false, "restore_ready": false})
}
