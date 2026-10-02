package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	mysqlstore "github.com/wongyiuming/FrontierCloud/internal/store/mysql"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

type maintenanceReport struct {
	Enabled         bool                       `json:"enabled"`
	NativeQuiescent bool                       `json:"native_quiescent"`
	Scope           string                     `json:"scope"`
	RestoreReady    bool                       `json:"restore_ready"`
	Database        *store.MaintenanceSnapshot `json:"database,omitempty"`
	PendingGates    []string                   `json:"pending_gates"`
}

func maintenanceCommand(arguments []string, output io.Writer) error {
	if len(arguments) == 0 {
		return errors.New("maintenance requires enter, status or resume")
	}
	action := arguments[0]
	if action != "enter" && action != "status" && action != "resume" {
		return errors.New("unknown maintenance action")
	}
	flags := flag.NewFlagSet("maintenance "+action, flag.ContinueOnError)
	wait := flags.Int("wait-seconds", 30, "bounded time to drain native processes sharing DATA_ROOT")
	if err := flags.Parse(arguments[1:]); err != nil {
		return err
	}
	if flags.NArg() != 0 || *wait < 1 || *wait > 300 {
		return errors.New("wait-seconds must be from 1 through 300")
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
	report := maintenanceReport{Scope: "native-processes-sharing-DATA_ROOT", PendingGates: []string{"all-legacy-and-remote-writers-stopped", "physical-journal-validation", "identity-and-ownership-remapping", "atomic-recovery-publication"}}
	check := func(ctx context.Context) error {
		db, err := openExistingStore(ctx, settings)
		if err != nil {
			return err
		}
		defer db.Close()
		snapshot, err := db.Maintenance().InspectMaintenance(ctx)
		if err != nil {
			return err
		}
		report.Database = &snapshot
		return nil
	}
	switch action {
	case "enter":
		err = gate.Enter(ctx, check)
		report.Enabled = true
	case "status":
		report.Enabled, err = gate.Enabled()
		if err == nil && report.Enabled {
			err = gate.Inspect(ctx, check)
		}
	case "resume":
		err = gate.Resume(ctx, check)
	}
	if err != nil {
		return fmt.Errorf("native maintenance proof failed; the gate was not successfully reopened: %w", err)
	}
	report.NativeQuiescent = report.Database != nil
	return json.NewEncoder(output).Encode(report)
}

// Inspection must not create or initialize an absent authoritative store.
func openExistingStore(ctx context.Context, settings config.Config) (store.Store, error) {
	if settings.DatabaseType == config.DatabaseSQLite {
		info, err := os.Stat(settings.SQLitePath)
		if err != nil || !info.Mode().IsRegular() {
			return nil, errors.New("existing SQLite store required for maintenance inspection")
		}
		return sqlitestore.OpenReadOnly(ctx, settings.SQLitePath)
	}
	return mysqlstore.OpenContext(ctx, mysqlstore.Config{
		Host: settings.MySQLHost, Port: settings.MySQLPort, Database: settings.MySQLDatabase,
		User: settings.MySQLUser, PasswordFile: settings.MySQLPasswordFile,
	})
}

// Initializers/migrations are writers too: acquire the same native lifecycle
// lease before changing files, identity or the selected schema.
func guardedCommand(settings config.Config, run func(context.Context) error) error {
	gate, err := maintenance.Open(settings.DataRoot)
	if err != nil {
		return err
	}
	defer gate.Close()
	runtime, err := gate.Runtime(context.Background())
	if err != nil {
		return err
	}
	defer runtime.Close()
	return run(runtime.Context())
}
