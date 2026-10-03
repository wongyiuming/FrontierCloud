package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/recording"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func recordingAdoptionCommand(arguments []string, output io.Writer, export bool) error {
	name := "adopt-recordings"
	if export {
		name = "recording-inventory"
	}
	flags := flag.NewFlagSet(name, flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	relationship := flags.String("relationship", "", "current relationship ID")
	confirmation := flags.String("confirm-node-id", "", "exact current local identity")
	inventory := flags.String("file", "", "signed Master inventory; required for adoption")
	wait := flags.Int("wait-seconds", 300, "bounded offline scan deadline")
	if flags.Parse(arguments) != nil || flags.NArg() != 0 || !node.ValidIdentifier(*confirmation) || !node.ValidIdentifier(*relationship) || *wait < 1 || *wait > 300 || export && *inventory != "" || !export && *inventory == "" {
		return errors.New("recording maintenance requires relationship, confirm-node-id, valid wait-seconds and an inventory file only for adoption")
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
	var result []byte
	err = gate.Inspect(ctx, func(ctx context.Context) error {
		if err := bindStore(ctx, c); err != nil {
			return err
		}
		db, err := openExistingStore(ctx, c)
		if err != nil {
			return err
		}
		if !export && c.DatabaseType == config.DatabaseSQLite {
			if err := db.Close(); err != nil {
				return err
			}
			db, err = sqlitestore.OpenExisting(ctx, c.SQLitePath)
			if err != nil {
				return err
			}
		}
		defer db.Close()
		snapshot, err := db.Maintenance().InspectMaintenance(ctx)
		if err != nil {
			return err
		}
		if !snapshot.LogicalIdle {
			return store.ErrBackupBusy
		}
		identity, err := node.OpenExisting(ctx, db.Nodes(), c.SecretsDirectory)
		if err != nil || identity.ID != *confirmation {
			return store.ErrNodeState
		}
		if export {
			if identity.Role != "Master" {
				return store.ErrNodeState
			}
			result, err = identity.RecordingInventory(ctx, db.Maintenance(), db.Nodes(), *relationship, time.Now().Unix())
			return err
		}
		if identity.Role != "Follower" {
			return store.ErrNodeState
		}
		before, err := os.Lstat(*inventory)
		if err != nil {
			return err
		}
		if !before.Mode().IsRegular() || before.Size() <= 0 || before.Size() > store.MaxRecordingInventoryBytes {
			return store.ErrRecordingState
		}
		file, err := os.Open(*inventory)
		if err != nil {
			return err
		}
		defer file.Close()
		opened, err := file.Stat()
		if err != nil || !os.SameFile(before, opened) {
			return store.ErrRecordingState
		}
		signed, err := recording.ParseInventory(file)
		if err != nil {
			return err
		}
		if signed.Payload.Relationship != *relationship {
			return store.ErrNodeState
		}
		adopted, err := recording.AdoptOwnedRecordings(ctx, filepath.Join(c.DataRoot, "recordings"), db.Maintenance(), db.Recordings(), db.Nodes(), signed, *confirmation)
		if err != nil {
			return err
		}
		result, err = json.Marshal(map[string]any{"adopted_recordings": adopted, "maintenance_enabled": true, "scope": "native-processes-sharing-DATA_ROOT", "restore_ready": false})
		return err
	})
	if err != nil {
		return err
	}
	_, err = output.Write(append(result, '\n'))
	return err
}
