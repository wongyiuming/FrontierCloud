// Package mysql implements the MySQL authoritative store backend.
package mysql

import (
	"context"
	"database/sql"
	"fmt"
	"net"
	"os"
	"strings"
	"time"

	driver "github.com/go-sql-driver/mysql"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"github.com/wongyiuming/FrontierCloud/internal/store/business"
	"github.com/wongyiuming/FrontierCloud/internal/store/schema"
)

// Config contains MySQL connection details without exposing them to protocol code.
type Config struct {
	Host         string
	Port         int
	Database     string
	User         string
	PasswordFile string
}

// Store owns the MySQL connection pool.
type Store struct {
	database *sql.DB
}

func Open(value Config) (*Store, error) {
	password, err := os.ReadFile(value.PasswordFile)
	if err != nil {
		return nil, fmt.Errorf("read MySQL password file: %w", err)
	}
	configuration := driver.NewConfig()
	configuration.User = value.User
	configuration.Passwd = strings.TrimSpace(string(password))
	configuration.Net = "tcp"
	configuration.Addr = net.JoinHostPort(value.Host, fmt.Sprint(value.Port))
	configuration.DBName = value.Database
	configuration.ParseTime = true
	configuration.Loc = time.UTC
	configuration.Collation = "utf8mb4_unicode_ci"
	configuration.Params = map[string]string{"charset": "utf8mb4"}
	configuration.Timeout = 5 * time.Second
	configuration.ReadTimeout = 5 * time.Second
	configuration.WriteTimeout = 5 * time.Second
	database, err := sql.Open("mysql", configuration.FormatDSN())
	if err != nil {
		return nil, fmt.Errorf("open MySQL: %w", err)
	}
	database.SetMaxOpenConns(16)
	database.SetMaxIdleConns(4)
	database.SetConnMaxIdleTime(5 * time.Minute)
	store := &Store{database: database}
	if err := store.Ping(context.Background()); err != nil {
		_ = database.Close()
		return nil, err
	}
	return store, nil
}

func (store *Store) Backend() string {
	return "mysql"
}

func (store *Store) Initialize(ctx context.Context) error {
	return schema.Initialize(ctx, store.database, store.Backend())
}

func (store *Store) Ping(ctx context.Context) error {
	if err := store.database.PingContext(ctx); err != nil {
		return fmt.Errorf("ping MySQL: %w", err)
	}
	return nil
}

func (store *Store) Close() error {
	return store.database.Close()
}

func (s *Store) Media() store.MediaRepository { return business.New(s.database, s.Backend()) }
func (s *Store) Nodes() store.NodeRepository  { return business.New(s.database, s.Backend()) }
func (s *Store) Admin() store.AdminRepository { return business.New(s.database, s.Backend()) }

func (s *Store) Observations() store.ObservationRepository {
	return business.New(s.database, s.Backend())
}

func (s *Store) Security() store.SecurityRepository { return business.New(s.database, s.Backend()) }

// Infrastructure/test access only; deliberately absent from the Store contract
// supplied to services and handlers.
func (s *Store) Database() *sql.DB { return s.database }
