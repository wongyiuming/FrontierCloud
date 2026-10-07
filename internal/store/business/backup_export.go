package business

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/store"
	"github.com/wongyiuming/FrontierCloud/migrations"
)

// The v2 compatibility artifact excludes identity/private keys, relationship
// credentials, cold backups and runtime mutation journals. Never SELECT caller
// supplied table names or substitute this artifact for the online database.
func (r *Repository) ExportBusinessSnapshot(ctx context.Context, emit func(string, map[string]any) error) error {
	if emit == nil {
		return store.ErrBackupState
	}
	level := sql.LevelSerializable
	if r.backend == "mysql" {
		level = sql.LevelRepeatableRead
	}
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{Isolation: level, ReadOnly: true})
	if err != nil {
		return err
	}
	defer tx.Rollback()
	node, err := readNode(ctx, tx, "")
	if err != nil {
		return err
	}
	if node.Role != "Master" {
		return store.ErrNodeState
	}
	var generation int
	if err := tx.QueryRowContext(ctx, "SELECT generation FROM frontiercloud_schema WHERE singleton=1").Scan(&generation); err != nil {
		return err
	}
	if generation != migrations.Generation {
		return store.ErrBackupState
	}
	// A v2 artifact has no replay journals. Refuse a snapshot whose capacity or
	// namespace still depends on an omitted intent, rather than losing it.
	for _, query := range []string{
		"SELECT COUNT(*) FROM media_delete_operations WHERE state<>'rename_done'",
		"SELECT COUNT(*) FROM cluster_upload_sessions WHERE state NOT IN ('complete','cancelled')",
		"SELECT COUNT(*) FROM global_media_objects WHERE state<>'active'",
		"SELECT COUNT(*) FROM karaoke_recordings WHERE state<>'ready'",
		"SELECT COUNT(*) FROM karaoke_users WHERE status='deleting'",
		"SELECT COUNT(*) FROM cluster_storage_members WHERE reserved_bytes<>0",
	} {
		var count int64
		if err := tx.QueryRowContext(ctx, query).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return store.ErrBackupBusy
		}
	}
	for _, table := range store.BusinessBackupTables() {
		if err := exportBackupTable(ctx, tx, table, emit); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func exportBackupTable(ctx context.Context, tx *sql.Tx, table string, emit func(string, map[string]any) error) error {
	rows, err := tx.QueryContext(ctx, "SELECT * FROM "+table)
	if err != nil {
		return err
	}
	defer rows.Close()
	columns, err := rows.ColumnTypes()
	if err != nil {
		return err
	}
	for rows.Next() {
		if err := ctx.Err(); err != nil {
			return err
		}
		values := make([]any, len(columns))
		pointers := make([]any, len(columns))
		for i := range values {
			pointers[i] = &values[i]
		}
		if err := rows.Scan(pointers...); err != nil {
			return err
		}
		value := make(map[string]any, len(columns))
		for i, column := range columns {
			normalized, err := backupValue(values[i], column.DatabaseTypeName())
			if err != nil {
				return err
			}
			value[column.Name()] = normalized
		}
		if err := emit(table, value); err != nil {
			return err
		}
	}
	return rows.Err()
}

func backupValue(value any, kind string) (any, error) {
	if value == nil {
		return nil, nil
	}
	kind = strings.ToUpper(kind)
	if strings.HasPrefix(kind, "DATETIME") || strings.HasPrefix(kind, "TIMESTAMP") {
		var date sqlDate
		if err := date.Scan(value); err != nil {
			return nil, store.ErrBackupState
		}
		return map[string]string{"$datetime": isoTime(date.Time, false)}, nil
	}
	switch v := value.(type) {
	case time.Time:
		return map[string]string{"$datetime": isoTime(v, false)}, nil
	case []byte:
		if len(v) > 4*1024*1024 {
			return nil, store.ErrBackupState
		}
		if strings.Contains(kind, "BLOB") || strings.Contains(kind, "BINARY") || kind == "BIT" {
			return map[string]string{"$bytes": base64.StdEncoding.EncodeToString(v)}, nil
		}
		return backupValue(string(v), kind)
	case string:
		if len(v) > 4*1024*1024 || !utf8.ValidString(v) {
			return nil, store.ErrBackupState
		}
		if strings.Contains(kind, "INT") {
			if _, err := strconv.ParseInt(v, 10, 64); err != nil {
				if _, err := strconv.ParseUint(v, 10, 64); err != nil {
					return nil, store.ErrBackupState
				}
			}
			return json.Number(v), nil
		}
		return v, nil
	case int64, int32, int, bool:
		return v, nil
	case uint64:
		return json.Number(strconv.FormatUint(v, 10)), nil
	case float64:
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return nil, store.ErrBackupState
		}
		return v, nil
	default:
		return nil, store.ErrBackupState
	}
}

func (r *Repository) RecordBackupDelivery(ctx context.Context, relationship string, generation int64, checksum string, a store.NodeAudit) error {
	if generation <= 0 || !nodeIDPattern.MatchString(relationship) || !nodeHashPattern.MatchString(checksum) {
		return store.ErrBackupState
	}
	return r.write(ctx, func(q queryer) error {
		node, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if node.Role != "Master" {
			return store.ErrNodeState
		}
		rel, err := scanRelation(q.QueryRowContext(ctx, "SELECT "+relationColumns+" FROM node_relationships WHERE relationship_id=?"+r.lock(), relationship))
		if errors.Is(err, sql.ErrNoRows) {
			return store.ErrNodeState
		}
		if err != nil {
			return err
		}
		if rel.State != "active" || rel.Direction != "downstream" || rel.Protocol != 2 {
			return store.ErrNodeState
		}
		var enabled int
		var previous int64
		var previousChecksum string
		if err := q.QueryRowContext(ctx, "SELECT enabled,generation,checksum FROM cluster_backup_members WHERE member_id=?"+r.lock(), rel.PeerID).Scan(&enabled, &previous, &previousChecksum); err != nil {
			return err
		}
		if enabled != 1 {
			return store.ErrNodeState
		}
		if previous > generation {
			return store.ErrBackupState
		}
		if previous == generation {
			if previousChecksum == checksum {
				return nil
			}
			return store.ErrBackupState
		}
		now := time.Now().Unix()
		if _, err := q.ExecContext(ctx, "UPDATE cluster_backup_members SET generation=?,checksum=?,last_success=?,lag_seconds=0,state='ready',updated_at=? WHERE member_id=?", generation, checksum, now, now, rel.PeerID); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "backup-delivered", relationship, map[string]any{"generation": generation, "checksum": checksum}, a)
	})
}
