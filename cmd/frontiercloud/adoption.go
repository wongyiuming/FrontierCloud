package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	"io"
	"path/filepath"
	"time"
)

func adoptStorageCommand(arguments []string, output io.Writer) error {
	flags := flag.NewFlagSet("adopt-storage", flag.ContinueOnError)
	relationship := flags.String("relationship", "", "current active upstream relationship, never a historical backup ID")
	confirmation := flags.String("confirm-node-id", "", "exact current Follower identity")
	wait := flags.Int("wait-seconds", 300, "bounded local native maintenance/physical scan deadline")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	if flags.NArg() != 0 || !node.ValidIdentifier(*relationship) || !node.ValidIdentifier(*confirmation) || *wait < 1 || *wait > 300 {
		return errors.New("adopt-storage requires relationship, confirm-node-id and wait-seconds from 1 through 300")
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
	var adopted int
	err = gate.Inspect(ctx, func(ctx context.Context) error {
		if err := bindStore(ctx, settings); err != nil {
			return err
		}
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
		if snapshot.Role != "Follower" || !snapshot.LogicalIdle {
			return store.ErrBackupBusy
		}
		adopted, err = media.AdoptOwnedStorage(ctx, filepath.Join(settings.DataRoot, "media"), db.Maintenance(), db.Media(), db.Nodes(), *relationship, *confirmation)
		return err
	})
	if err != nil {
		return err
	}
	return json.NewEncoder(output).Encode(map[string]any{"adopted_objects": adopted, "native_quiescent": true, "scope": "native-processes-sharing-DATA_ROOT", "maintenance_enabled": true, "restore_ready": false})
}
