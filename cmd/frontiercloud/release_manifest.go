package main

import (
	"context"
	"errors"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/release"
)

// Invoked by the isolated updater in the newly healthy native Master. Whole
// manifests travel as bounded argv, never a mutable mounted working-tree file.
func clusterManifestCommand(arguments []string) error {
	if len(arguments) != 2 || len(arguments[0]) > 11000 || (arguments[1] != "upgrade" && arguments[1] != "rollback") {
		return errors.New("cluster-release-manifest requires a bounded encoded manifest and upgrade or rollback")
	}
	raw, err := protocol.Decode(arguments[0])
	if err != nil {
		return release.ErrManifest
	}
	manifest, err := release.ParseManifest(raw)
	if err != nil {
		return err
	}
	c, err := config.Load()
	if err != nil {
		return err
	}
	return guardedCommand(c, func(ctx context.Context) error {
		db, err := openExistingStore(ctx, c)
		if err != nil {
			return err
		}
		defer db.Close()
		if _, err = db.Maintenance().InspectMaintenance(ctx); err != nil {
			return err
		}
		identity, err := node.OpenExisting(ctx, db.Nodes(), c.SecretsDirectory)
		if err != nil {
			return err
		}
		transport := node.NewTransport()
		defer transport.Close()
		control := node.NewService(db.Nodes(), identity, transport)
		coordinator := release.Coordinator{Nodes: db.Nodes(), Control: control, Policy: release.Policy{Branch: c.ReleaseBranch, Source: c.ReleaseSourceBranch}}
		return coordinator.ConvergeManifest(ctx, manifest, arguments[1])
	})
}
