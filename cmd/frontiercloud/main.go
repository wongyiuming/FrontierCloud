package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/backup"
	"github.com/wongyiuming/FrontierCloud/internal/bootstrap"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/recording"
	"github.com/wongyiuming/FrontierCloud/internal/security"
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
		return guardedCommand(settings, func(parent context.Context) error {
			database, err := openRuntimeStore(parent, settings)
			if err != nil {
				return err
			}
			defer database.Close()
			ctx, cancel := context.WithTimeout(parent, 90*time.Second)
			defer cancel()
			return database.Initialize(ctx)
		})
	case "init-secrets":
		settings, err := config.Load()
		if err != nil {
			return err
		}
		return guardedCommand(settings, func(context.Context) error { return bootstrap.InitializeSecrets(settings.SecretsDirectory) })
	case "init-media":
		settings, err := config.Load()
		if err != nil {
			return err
		}
		return guardedCommand(settings, func(ctx context.Context) error { return bootstrap.InitializeMediaContext(ctx, settings.DataRoot) })
	case "healthcheck":
		return healthcheck()
	case "verify-backup":
		return verifyBackupCommand(arguments[1:], os.Stdout)
	case "cleanup-backup-cache":
		return cleanupBackupCacheCommand(arguments[1:], os.Stdout)
	case "maintenance":
		return maintenanceCommand(arguments[1:], os.Stdout)
	case "master-migration":
		return masterMigrationCommand(arguments[1:], os.Stdout)
	case "adopt-storage":
		return adoptStorageCommand(arguments[1:], os.Stdout)
	case "adopt-recordings":
		return recordingAdoptionCommand(arguments[1:], os.Stdout, false)
	case "recording-inventory":
		return recordingAdoptionCommand(arguments[1:], os.Stdout, true)
	case "drain-rename-history":
		return drainRenameCommand(arguments[1:], os.Stdout)
	case "prepare-release":
		return prepareReleaseCommand(arguments[1:])
	case "cluster-release":
		return clusterReleaseCommand(arguments[1:])
	case "cluster-release-manifest":
		return clusterManifestCommand(arguments[1:])
	case "updater-status":
		if len(arguments) != 1 {
			return errors.New("updater-status accepts no arguments")
		}
		return updaterStatusCommand(os.Stdout)
	default:
		return fmt.Errorf("unknown command %q", name)
	}
}

func serve() error {
	settings, err := config.Load()
	if err != nil {
		return err
	}
	level := slog.LevelInfo
	switch settings.LogLevel {
	case "DEBUG":
		level = slog.LevelDebug
	case "WARNING":
		level = slog.LevelWarn
	case "ERROR":
		level = slog.LevelError
	case "CRITICAL":
		level = slog.Level(12)
	}
	options := &slog.HandlerOptions{Level: level}
	var logging slog.Handler = slog.NewJSONHandler(os.Stdout, options)
	if settings.LogFormat == "text" {
		logging = slog.NewTextHandler(os.Stdout, options)
	}
	slog.SetDefault(slog.New(logging))
	gate, err := maintenance.Open(settings.DataRoot)
	if err != nil {
		return err
	}
	defer gate.Close()
	parent, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	runtime, err := gate.Runtime(parent)
	if err != nil {
		return err
	}
	defer runtime.Close()
	shutdown := runtime.Context()
	database, err := openRuntimeStore(shutdown, settings)
	if err != nil {
		return err
	}
	defer database.Close()
	initialization, cancelInitialization := context.WithTimeout(shutdown, 90*time.Second)
	defer cancelInitialization()
	if err := database.Initialize(initialization); err != nil {
		return err
	}
	identity, err := node.Initialize(initialization, database.Nodes(), settings.SecretsDirectory)
	if err != nil {
		return err
	}
	if err := runtimeIdentityTransport(settings, identity); err != nil {
		return err
	}
	if err := identity.CheckNativeRuntime(initialization, settings.DataRoot); err != nil {
		return err
	}
	mediaService, err := media.NewContext(shutdown, filepath.Join(settings.DataRoot, "media"), database.Media(), identity)
	if err != nil {
		return err
	}
	defer mediaService.Close()

	redisOptions, err := redis.ParseURL(settings.RedisURL)
	if err != nil {
		return err
	}
	redisOptions.ContextTimeoutEnabled = true
	redisOptions.MaxRetries = 1
	redisOptions.DialTimeout = 2 * time.Second
	redisOptions.ReadTimeout = 2 * time.Second
	redisOptions.WriteTimeout = 2 * time.Second
	redisClient := redis.NewClient(redisOptions)
	defer redisClient.Close()

	resolver, err := network.New(settings.TrustedProxyNetworks)
	if err != nil {
		return err
	}
	controlTransport := node.NewTransport()
	defer controlTransport.Close()
	controlService := node.NewService(database.Nodes(), identity, controlTransport)
	mediaService.ConfigureCluster(database.Nodes(), database.Pool(), controlService)
	recordingsRoot, err := os.OpenRoot(filepath.Join(settings.DataRoot, "recordings"))
	if err != nil {
		return err
	}
	defer recordingsRoot.Close()
	controlService.ConfigureVolumes(database.Pool(), mediaService, recordingsRoot)
	backupBuilder, err := backup.New(database.Backups(), mediaService, filepath.Join(settings.DataRoot, ".business-backups"))
	if err != nil {
		return err
	}
	defer backupBuilder.Close()
	controlService.ConfigureBackups(database.Backups(), backupBuilder)
	recordingStorage, err := recording.NewContext(shutdown, recordingsRoot, database.Recordings(), database.Nodes())
	if err != nil {
		return err
	}
	recordingManager := recording.NewManager(database.Recordings(), database.Karaoke(), database.Nodes(), database.Pool(), controlService, recordingStorage)
	securityService, err := security.New(settings, database.Security())
	if err != nil {
		return err
	}
	defer securityService.Close()
	edgeInit, edgeCancel := context.WithTimeout(shutdown, 15*time.Second)
	err = securityService.Publish(edgeInit, true)
	edgeCancel()
	if err != nil {
		return err
	}
	handler, closeHTTP, err := newRuntimeHTTP(settings, runtimeHTTP{Database: database, Identity: identity, Media: mediaService, Control: controlService, Recordings: recordingStorage, Manager: recordingManager, Security: securityService, Redis: redisClient, Resolver: resolver})
	if err != nil {
		return err
	}
	defer closeHTTP()
	admission, cancelAdmission := context.WithTimeout(shutdown, 15*time.Second)
	err = identity.RecordNativeRuntime(admission, settings.DataRoot)
	cancelAdmission()
	if err != nil {
		return err
	}
	server := &http.Server{
		Addr:              settings.HTTPAddress,
		Handler:           runtime.Handler(handler),
		BaseContext:       func(net.Listener) context.Context { return shutdown },
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	publisherDone := make(chan struct{})
	go func() { defer close(publisherDone); securityService.Run(shutdown) }()
	deletionDone := make(chan struct{})
	go func() { defer close(deletionDone); mediaService.RunGlobalDeletes(shutdown) }()
	recordingDone := make(chan struct{})
	go func() { defer close(recordingDone); recordingManager.Run(shutdown) }()
	backupDone := make(chan struct{})
	go func() { defer close(backupDone); controlService.RunBackups(shutdown) }()
	controlDone := make(chan struct{})
	go func() { defer close(controlDone); controlService.Run(shutdown) }()
	defer func() {
		stop()
		<-publisherDone
		<-deletionDone
		<-recordingDone
		<-backupDone
		<-controlDone
	}()
	// Cancel handlers, close network bodies, then join them BEFORE service roots
	// or the lifecycle lease close. Even ignored-context cleanup holds the fence.
	defer func() { runtime.Stop(); server.Close(); runtime.WaitHTTP() }()
	if err := shutdown.Err(); err != nil {
		return err
	}
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

func runtimeIdentityTransport(settings config.Config, identity *node.Identity) error {
	if identity == nil {
		return storecontract.ErrNodeState
	}
	switch identity.Role {
	case "Standalone":
		return nil
	case "Master", "Follower":
		if !settings.TLSEnabled {
			return errors.New("fixed Master/Follower identity requires HTTPS; restore TLS_ENABLED, role is never reset automatically")
		}
		if _, err := node.Endpoint(identity.Endpoint); err != nil {
			return err
		}
		return nil
	default:
		return storecontract.ErrNodeState
	}
}

func healthcheck() error {
	settings, err := config.Load()
	if err != nil {
		return err
	}
	address, err := healthAddress(settings.HTTPAddress)
	if err != nil {
		return err
	}
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Get("http://" + address + "/health/ready")
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("health endpoint returned %d", response.StatusCode)
	}
	return nil
}

func healthAddress(address string) (string, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return "", fmt.Errorf("invalid HTTP_ADDR: %w", err)
	}
	if host == "" || host == "0.0.0.0" {
		host = "127.0.0.1"
	}
	if host == "::" {
		host = "::1"
	}
	return net.JoinHostPort(host, port), nil
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
