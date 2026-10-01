package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/admin"
	"github.com/wongyiuming/FrontierCloud/internal/bootstrap"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/httpapi"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	storecontract "github.com/wongyiuming/FrontierCloud/internal/store"
	mysqlstore "github.com/wongyiuming/FrontierCloud/internal/store/mysql"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	"path/filepath"
)

func main() {
	if err := command(os.Args[1:]); err != nil {
		slog.Error("FrontierCloud stopped", "error", err)
		os.Exit(1)
	}
}

func command(arguments []string) error {
	name := "serve"
	if len(arguments) > 0 {
		name = arguments[0]
	}
	switch name {
	case "serve":
		return serve()
	case "migrate":
		settings, err := config.Load()
		if err != nil {
			return err
		}
		database, err := openStore(settings)
		if err != nil {
			return err
		}
		defer database.Close()
		ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		defer cancel()
		return database.Initialize(ctx)
	case "init-secrets":
		settings, err := config.Load()
		if err != nil {
			return err
		}
		return bootstrap.InitializeSecrets(settings.SecretsDirectory)
	case "init-media":
		settings, err := config.Load()
		if err != nil {
			return err
		}
		return bootstrap.InitializeMedia(settings.DataRoot)
	case "healthcheck":
		return healthcheck()
	default:
		return fmt.Errorf("unknown command %q", name)
	}
}

func serve() error {
	settings, err := config.Load()
	if err != nil {
		return err
	}
	database, err := openStore(settings)
	if err != nil {
		return err
	}
	defer database.Close()
	initialization, cancelInitialization := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancelInitialization()
	if err := database.Initialize(initialization); err != nil {
		return err
	}
	identity, err := node.Initialize(initialization, database.Nodes(), settings.SecretsDirectory)
	if err != nil {
		return err
	}
	// Refuse existing cluster roles until their full control/storage implementation
	// is available. A partial runtime must never silently take over a Master.
	if identity.Role != "Standalone" {
		return fmt.Errorf("Go cluster runtime is not yet complete for existing %s nodes", identity.Role)
	}
	mediaService, err := media.New(filepath.Join(settings.DataRoot, "media"), database.Media(), identity)
	if err != nil {
		return err
	}
	defer mediaService.Close()

	redisOptions, err := redis.ParseURL(settings.RedisURL)
	if err != nil {
		return err
	}
	redisClient := redis.NewClient(redisOptions)
	defer redisClient.Close()

	resolver, err := network.New(settings.TrustedProxyNetworks)
	if err != nil {
		return err
	}
	handler := httpapi.NewWithResolver(database.Ping, func(ctx context.Context) error {
		return redisClient.Ping(ctx).Err()
	}, resolver)
	public, err := httpapi.RegisterPublic(handler, settings, mediaService)
	if err != nil {
		return err
	}
	defer public.Close()
	adminService, err := admin.New(settings, admin.NewRedisCache(redisClient), database.Admin())
	if err != nil {
		return err
	}
	if _, err := httpapi.RegisterAdmin(handler, settings, adminService, public, identity); err != nil {
		return err
	}
	server := &http.Server{
		Addr:              settings.HTTPAddress,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	shutdown, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	errorsChannel := make(chan error, 1)
	go func() { errorsChannel <- server.ListenAndServe() }()
	slog.Info("FrontierCloud Go runtime started", "address", settings.HTTPAddress, "database", database.Backend())
	select {
	case err := <-errorsChannel:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	case <-shutdown.Done():
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return server.Shutdown(ctx)
	}
}

func healthcheck() error {
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Get("http://127.0.0.1:8000/health/ready")
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("health endpoint returned %d", response.StatusCode)
	}
	return nil
}

func openStore(settings config.Config) (storecontract.Store, error) {
	if settings.DatabaseType == config.DatabaseSQLite {
		return sqlitestore.Open(settings.SQLitePath)
	}
	return mysqlstore.Open(mysqlstore.Config{
		Host:         settings.MySQLHost,
		Port:         settings.MySQLPort,
		Database:     settings.MySQLDatabase,
		User:         settings.MySQLUser,
		PasswordFile: settings.MySQLPasswordFile,
	})
}
