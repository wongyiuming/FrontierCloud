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
	"github.com/wongyiuming/FrontierCloud/internal/admin"
	"github.com/wongyiuming/FrontierCloud/internal/backup"
	"github.com/wongyiuming/FrontierCloud/internal/bootstrap"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/httpapi"
	"github.com/wongyiuming/FrontierCloud/internal/karaoke"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/observation"
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
	case "verify-backup":
		return verifyBackupCommand(arguments[1:], os.Stdout)
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
	recordingStorage, err := recording.New(recordingsRoot, database.Recordings(), database.Nodes())
	if err != nil {
		return err
	}
	recordingManager := recording.NewManager(database.Recordings(), database.Karaoke(), database.Nodes(), database.Pool(), controlService, recordingStorage)
	securityService, err := security.New(settings, database.Security())
	if err != nil {
		return err
	}
	defer securityService.Close()
	edgeInit, edgeCancel := context.WithTimeout(context.Background(), 15*time.Second)
	err = securityService.Publish(edgeInit, true)
	edgeCancel()
	if err != nil {
		return err
	}
	handler := httpapi.NewWithResolver(func(ctx context.Context) error {
		if err := database.Ping(ctx); err != nil {
			return err
		}
		if err := mediaService.Ready(ctx); err != nil {
			return err
		}
		if err := recordingStorage.Ready(ctx); err != nil {
			return err
		}
		return securityService.Ready(ctx)
	}, func(ctx context.Context) error {
		return redisClient.Ping(ctx).Err()
	}, resolver, httpapi.SecurityMiddleware(securityService, resolver))
	public, err := httpapi.RegisterPublic(handler, settings, mediaService)
	if err != nil {
		return err
	}
	defer public.Close()
	adminService, err := admin.New(settings, admin.NewRedisCache(redisClient), database.Admin())
	if err != nil {
		return err
	}
	adminHTTP, err := httpapi.RegisterAdmin(handler, settings, adminService, public, identity)
	if err != nil {
		return err
	}
	httpapi.RegisterSecurityAdmin(handler, adminHTTP, securityService)
	accountsHTTP := httpapi.RegisterKaraokeAccounts(handler, karaoke.New(database.Karaoke(), database.Nodes(), karaoke.NewRedisCache(redisClient)), public, adminHTTP, resolver)
	httpapi.RegisterKaraokeRecordings(handler, accountsHTTP, recordingManager, recordingStorage)
	httpapi.RegisterKaraokeMedia(handler, public, resolver)
	httpapi.RegisterNodeRecordings(handler, settings, resolver, controlService, recordingStorage)
	httpapi.RegisterNodeIdentity(handler, settings, resolver, controlService)
	httpapi.RegisterNodeControl(handler, settings, resolver, controlService)
	httpapi.RegisterNodeBackups(handler, settings, resolver, controlService, database.Backups())
	httpapi.RegisterNodeMedia(handler, settings, resolver, controlService, mediaService)
	httpapi.RegisterNodeStorage(handler, settings, resolver, controlService, mediaService)
	httpapi.RegisterObservations(handler, adminHTTP, observation.New(database.Observations(), redisClient, settings.WebRTCCooldown), resolver)
	server := &http.Server{
		Addr:              settings.HTTPAddress,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	shutdown, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	publisherDone := make(chan struct{})
	go func() { defer close(publisherDone); securityService.Run(shutdown) }()
	deletionDone := make(chan struct{})
	go func() { defer close(deletionDone); mediaService.RunGlobalDeletes(shutdown) }()
	recordingDone := make(chan struct{})
	go func() { defer close(recordingDone); recordingManager.Run(shutdown) }()
	backupDone := make(chan struct{})
	go func() { defer close(backupDone); controlService.RunBackups(shutdown) }()
	defer func() { stop(); <-publisherDone; <-deletionDone; <-recordingDone; <-backupDone }()
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
