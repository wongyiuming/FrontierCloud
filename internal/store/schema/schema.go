// Package schema initializes and upgrades the shared authoritative database schema.
package schema

import (
	"context"
	"database/sql"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/migrations"
)

var tablePattern = regexp.MustCompile("(?i)^CREATE TABLE(?: IF NOT EXISTS)?\\s+`?(\\w+)")

// Initialize reserves one connection for the migration lock and its statements.
func Initialize(ctx context.Context, database *sql.DB, backend string) error {
	statements, err := migrations.Statements(backend)
	if err != nil {
		return err
	}
	conn, err := database.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	if backend == "sqlite" {
		if _, err := conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
			return err
		}
	} else {
		var acquired int
		if err := conn.QueryRowContext(ctx, "SELECT GET_LOCK(CONCAT('frontiercloud:schema-migrations:', DATABASE()), 60)").Scan(&acquired); err != nil {
			return err
		}
		if acquired != 1 {
			return fmt.Errorf("schema migration lock timed out")
		}
		defer func() {
			release, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_, _ = conn.ExecContext(release, "SELECT RELEASE_LOCK(CONCAT('frontiercloud:schema-migrations:', DATABASE()))")
		}()
		if _, err := conn.ExecContext(ctx, "START TRANSACTION"); err != nil {
			return err
		}
	}
	defer func() {
		rollback, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, _ = conn.ExecContext(rollback, "ROLLBACK")
	}()
	tables, err := tableNames(ctx, conn, backend)
	if err != nil {
		return err
	}
	if len(tables) == 0 {
		for _, statement := range statements {
			if _, err := conn.ExecContext(ctx, statement); err != nil {
				return fmt.Errorf("initialize schema: %w", err)
			}
		}
		// MySQL DDL commits implicitly. Keep bootstrap singleton rows atomic.
		if backend == "mysql" {
			if _, err := conn.ExecContext(ctx, "START TRANSACTION"); err != nil {
				return err
			}
		}
		if _, err := conn.ExecContext(ctx, "INSERT INTO ip_security_projection(singleton) VALUES (1)"); err != nil {
			return err
		}
		if _, err := conn.ExecContext(ctx, "INSERT INTO frontiercloud_schema(singleton, generation, created_at) VALUES (1, ?, ?)", migrations.Generation, time.Now().Unix()); err != nil {
			return err
		}
	} else {
		if !tables["frontiercloud_schema"] {
			return fmt.Errorf("existing database has no schema marker; automatic adoption is unsupported")
		}
		var generation int
		if err := conn.QueryRowContext(ctx, "SELECT generation FROM frontiercloud_schema WHERE singleton=1").Scan(&generation); err != nil {
			return fmt.Errorf("read schema generation: %w", err)
		}
		if generation > migrations.Generation {
			return fmt.Errorf("database generation %d is newer than runtime generation %d; downgrade refused", generation, migrations.Generation)
		}
		if generation == 1 {
			for _, statement := range statements {
				if match := tablePattern.FindStringSubmatch(statement); len(match) == 2 && match[1] == "frontiercloud_schema_migrations" {
					if _, err := conn.ExecContext(ctx, statement); err != nil {
						return err
					}
				}
			}
			if backend == "mysql" {
				if _, err := conn.ExecContext(ctx, "START TRANSACTION"); err != nil {
					return err
				}
			}
			if _, err := conn.ExecContext(ctx, "DELETE FROM frontiercloud_schema_migrations WHERE generation=2"); err != nil {
				return err
			}
			if _, err := conn.ExecContext(ctx, "INSERT INTO frontiercloud_schema_migrations(generation, migration_name, checksum, applied_at) VALUES (2, ?, ?, ?)", migrations.JournalName, migrations.JournalChecksum, time.Now().Unix()); err != nil {
				return err
			}
			if _, err := conn.ExecContext(ctx, "UPDATE frontiercloud_schema SET generation=2 WHERE singleton=1"); err != nil {
				return err
			}
		} else if generation != migrations.Generation {
			return fmt.Errorf("unsupported schema generation %d", generation)
		}
	}
	tables, err = tableNames(ctx, conn, backend)
	if err != nil {
		return err
	}
	for _, statement := range statements {
		match := tablePattern.FindStringSubmatch(statement)
		if len(match) == 2 && !tables[match[1]] {
			return fmt.Errorf("incomplete schema: missing table %s", match[1])
		}
	}
	_, err = conn.ExecContext(ctx, "COMMIT")
	return err
}

func tableNames(ctx context.Context, conn *sql.Conn, backend string) (map[string]bool, error) {
	query := "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
	if backend == "mysql" {
		query = "SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema=DATABASE() AND TABLE_TYPE='BASE TABLE'"
	}
	rows, err := conn.QueryContext(ctx, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	names := map[string]bool{}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		names[strings.TrimSpace(name)] = true
	}
	return names, rows.Err()
}
