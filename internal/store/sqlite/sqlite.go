// Package sqlite implements the SQLite authoritative store backend.
package sqlite

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"

	"github.com/wongyiuming/FrontierCloud/internal/store/schema"

	_ "modernc.org/sqlite"
)

// Store owns a SQLite connection pool configured for multiple runtime workers.
type Store struct {
	database *sql.DB
}

// Open creates or opens a SQLite database with the FrontierCloud durability and
// concurrency baseline applied to every pooled connection.
func Open(path string) (*Store, error) {
	if path == "" {
		return nil, errors.New("SQLite path is empty")
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("resolve SQLite path: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(absolute), 0o750); err != nil {
		return nil, fmt.Errorf("create SQLite directory: %w", err)
	}
	databaseURL := &url.URL{Scheme: "file", Path: filepath.ToSlash(absolute)}
	query := databaseURL.Query()
	query.Set("_busy_timeout", "5000")
	query.Set("_defensive", "1")
	query.Set("_foreign_keys", "on")
	query.Set("_journal_mode", "WAL")
	query.Set("_synchronous", "NORMAL")
	databaseURL.RawQuery = query.Encode()

	database, err := sql.Open("sqlite", databaseURL.String())
	if err != nil {
		return nil, fmt.Errorf("open SQLite: %w", err)
	}
	database.SetMaxOpenConns(4)
	database.SetMaxIdleConns(4)
	store := &Store{database: database}
	if err := store.Ping(context.Background()); err != nil {
		_ = database.Close()
		return nil, err
	}
	return store, nil
}

func (store *Store) Backend() string {
	return "sqlite"
}

func (store *Store) Initialize(ctx context.Context) error {
	return schema.Initialize(ctx, store.database, store.Backend())
}

func (store *Store) Ping(ctx context.Context) error {
	if err := store.database.PingContext(ctx); err != nil {
		return fmt.Errorf("ping SQLite: %w", err)
	}
	return nil
}

func (store *Store) Close() error {
	return store.database.Close()
}

// Database is intentionally package-local infrastructure access. Handlers must
// depend on domain store interfaces rather than this connection pool.
func (store *Store) Database() *sql.DB {
	return store.database
}
