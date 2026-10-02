package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/bootstrap"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/release"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	"github.com/wongyiuming/FrontierCloud/migrations"
)

// Deliberately admits only the already supported generation. Updater rollback
// never attempts reverse DDL or opens an unknown newer schema with an old image.
func prepareReleaseCommand(arguments []string) error {
	flags := flag.NewFlagSet("prepare-release", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	generation := flags.Int("confirm-generation", 0, "exact compatible existing logical generation")
	if flags.Parse(arguments) != nil || flags.NArg() != 0 || *generation != migrations.Generation {
		return errors.New("prepare-release requires exact supported generation")
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
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	return gate.Inspect(ctx, func(ctx context.Context) error {
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
		if _, err = db.Maintenance().InspectMaintenance(ctx); err != nil {
			return err
		}
		// Initialize validates idempotent shared migrations; it cannot create a
		// missing store here or downgrade a future generation.
		if err = db.Initialize(ctx); err != nil {
			return err
		}
		if err = bootstrap.InitializeSecrets(settings.SecretsDirectory); err != nil {
			return err
		}
		return bootstrap.InitializeMediaContext(ctx, settings.DataRoot)
	})
}
func clusterReleaseCommand(arguments []string) error {
	if len(arguments) != 2 || !release.ValidSHA(arguments[0]) || (arguments[1] != "upgrade" && arguments[1] != "rollback") {
		return errors.New("cluster-release requires full target SHA and upgrade or rollback")
	}
	settings, err := config.Load()
	if err != nil {
		return err
	}
	return guardedCommand(settings, func(ctx context.Context) error {
		db, err := openExistingStore(ctx, settings)
		if err != nil {
			return err
		}
		defer db.Close()
		if _, err = db.Maintenance().InspectMaintenance(ctx); err != nil {
			return err
		}
		identity, err := node.OpenExisting(ctx, db.Nodes(), settings.SecretsDirectory)
		if err != nil {
			return err
		}
		transport := node.NewTransport()
		defer transport.Close()
		control := node.NewService(db.Nodes(), identity, transport)
		coordinator := release.Coordinator{Nodes: db.Nodes(), Control: control, Policy: release.Policy{Branch: settings.ReleaseBranch, Source: settings.ReleaseSourceBranch}}
		return coordinator.Converge(ctx, arguments[0], arguments[1])
	})
}

func updaterStatusCommand(output io.Writer) error {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	value, err := (release.SocketAgent{}).Request(ctx, map[string]any{"action": "status"})
	if err != nil || value["ok"] != true {
		return errors.New("updater status unavailable")
	}
	status, ok := value["status"].(map[string]any)
	if !ok {
		return errors.New("invalid updater status")
	}
	return json.NewEncoder(output).Encode(status)
}
