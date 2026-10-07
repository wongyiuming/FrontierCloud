package main

import (
	"context"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/deployment"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func bindStore(ctx context.Context, c config.Config) error {
	return deployment.Bind(ctx, c, func(ctx context.Context) error {
		db, err := openExistingStore(ctx, c)
		if err != nil {
			return err
		}
		defer db.Close()
		_, err = node.OpenExisting(ctx, db.Nodes(), c.SecretsDirectory)
		return err
	})
}

func openRuntimeStore(ctx context.Context, c config.Config) (store.Store, error) {
	if err := bindStore(ctx, c); err != nil {
		return nil, err
	}
	return openStore(c)
}
