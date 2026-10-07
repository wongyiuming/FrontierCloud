package business

import (
	"context"
	"database/sql"

	"github.com/wongyiuming/FrontierCloud/internal/store"
	"github.com/wongyiuming/FrontierCloud/migrations"
)

func (r *Repository) InspectMaintenance(ctx context.Context) (store.MaintenanceSnapshot, error) {
	var empty store.MaintenanceSnapshot
	level := sql.LevelSerializable
	if r.backend == "mysql" {
		level = sql.LevelRepeatableRead
	}
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{Isolation: level, ReadOnly: true})
	if err != nil {
		return empty, err
	}
	defer tx.Rollback()
	result := store.MaintenanceSnapshot{Pending: make(map[string]int64), LogicalIdle: true}
	if err := tx.QueryRowContext(ctx, "SELECT generation FROM frontiercloud_schema WHERE singleton=1").Scan(&result.SchemaGeneration); err != nil {
		return empty, err
	}
	if result.SchemaGeneration != migrations.Generation {
		return empty, store.ErrBackupState
	}
	if err := tx.QueryRowContext(ctx, "SELECT `role` FROM node_identity WHERE singleton=1").Scan(&result.Role); err != nil {
		return empty, err
	}
	if result.Role != "Standalone" && result.Role != "Master" && result.Role != "Follower" {
		return empty, store.ErrNodeState
	}
	for _, check := range []struct{ name, query string }{
		{"media_mutations", "SELECT COUNT(*) FROM media_delete_operations WHERE state<>'rename_done'"},
		{"uploads", "SELECT COUNT(*) FROM cluster_upload_sessions WHERE state NOT IN ('complete','cancelled')"},
		{"placements", "SELECT COUNT(*) FROM global_media_objects WHERE state<>'active'"},
		{"recordings", "SELECT COUNT(*) FROM karaoke_recordings WHERE state NOT IN ('ready','deleted')"},
		{"accounts", "SELECT COUNT(*) FROM karaoke_users WHERE status NOT IN ('active','banned','deleted')"},
		{"reserved_storage", "SELECT COUNT(*) FROM cluster_storage_members WHERE reserved_bytes<>0"},
		{"cold_transfers", "SELECT COUNT(*) FROM cluster_business_backups WHERE state NOT IN ('ready','failed')"},
		{"worker_leases", "SELECT COUNT(*) FROM cluster_worker_jobs WHERE state NOT IN ('queued','complete')"},
	} {
		var count int64
		if err := tx.QueryRowContext(ctx, check.query).Scan(&count); err != nil {
			return empty, err
		}
		result.Pending[check.name] = count
		if count != 0 {
			result.LogicalIdle = false
		}
	}
	if err := tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM media_delete_operations WHERE state='rename_done'").Scan(&result.CompletedNativeRenames); err != nil {
		return empty, err
	}
	if err := tx.Commit(); err != nil {
		return empty, err
	}
	return result, nil
}
