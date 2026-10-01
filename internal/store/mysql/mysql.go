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
	configuration := driver.Config{
		User:      value.User,
		Passwd:    strings.TrimSpace(string(password)),
		Net:       "tcp",
		Addr:      net.JoinHostPort(value.Host, fmt.Sprint(value.Port)),
		DBName:    value.Database,
		ParseTime: true,
		Loc:       time.UTC,
		Collation: "utf8mb4_unicode_ci",
		Params:    map[string]string{"charset": "utf8mb4"},
	}
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

func (store *Store) Ping(ctx context.Context) error {
	if err := store.database.PingContext(ctx); err != nil {
		return fmt.Errorf("ping MySQL: %w", err)
	}
	return nil
}

func (store *Store) Close() error {
	return store.database.Close()
}
