package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"sort"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// Exact binary path scopes on BOTH backends. SQLite LIKE alone is normally
// case-insensitive, even on a binary-collated column.
func (r *Repository) pathScope(column, path string, directory bool) (string, []any) {
	exact := column + "=?"
	args := []any{path}
	if r.backend == "mysql" {
		exact = "(" + column + "=? AND BINARY " + column + "=BINARY ?)"
		args = append(args, path)
	}
	if !directory {
		return exact, args
	}
	prefix := path + "/"
	binaryPrefix := "substr(" + column + ",1,length(?))=?"
	if r.backend == "mysql" {
		binaryPrefix = "BINARY LEFT(" + column + ",CHAR_LENGTH(?))=BINARY ?"
	}
	return "(" + exact + " OR (" + column + " LIKE ? ESCAPE '!' AND " + binaryPrefix + "))", append(args, escapeLike(prefix)+"%", prefix, prefix)
}

func deletePaths(items []store.DeleteItem) []string {
	paths := make([]string, 0, len(items))
	for _, item := range items {
		paths = append(paths, item.Path)
	}
	return paths
}

func (r *Repository) PrepareDelete(ctx context.Context, operation store.DeleteOperation, audit store.AdminAudit) error {
	manifest, err := json.Marshal(operation.Items)
	if err != nil {
		return err
	}
	return r.write(ctx, func(q queryer) error {
		if _, err := q.ExecContext(ctx, "INSERT INTO media_delete_operations (operation_id,state,manifest,created_at) VALUES (?,'pending',?,?)", operation.ID, string(manifest), timestamp(time.Now())); err != nil {
			return err
		}
		audit.Result = "pending"
		detail, _ := json.Marshal(map[string]any{"operation_id": operation.ID})
		audit.Detail = string(detail)
		return appendMutationAudit(ctx, q, audit, deletePaths(operation.Items))
	})
}

func (r *Repository) DeleteOperation(ctx context.Context, id string) (*store.DeleteOperation, error) {
	return r.deleteOperation(ctx, r.db, id, false)
}
func (r *Repository) deleteOperation(ctx context.Context, q queryer, id string, lock bool) (*store.DeleteOperation, error) {
	value := store.DeleteOperation{ID: id}
	var manifest []byte
	query := "SELECT state,manifest FROM media_delete_operations WHERE operation_id=?"
	if lock {
		query += r.lock()
	}
	err := q.QueryRowContext(ctx, query, id).Scan(&value.State, &manifest)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(manifest, &value.Items); err != nil {
		return nil, err
	}
	return &value, nil
}
func (r *Repository) DeleteOperations(ctx context.Context) ([]store.DeleteOperation, error) {
	rows, err := r.db.QueryContext(ctx, "SELECT operation_id,state,manifest FROM media_delete_operations ORDER BY created_at,operation_id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []store.DeleteOperation{}
	for rows.Next() {
		var value store.DeleteOperation
		var manifest []byte
		if err := rows.Scan(&value.ID, &value.State, &manifest); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(manifest, &value.Items); err != nil {
			return nil, err
		}
		result = append(result, value)
	}
	return result, rows.Err()
}
func (r *Repository) ForgetDelete(ctx context.Context, id string) error {
	return r.write(ctx, func(q queryer) error {
		_, err := q.ExecContext(ctx, "DELETE FROM media_delete_operations WHERE operation_id=?", id)
		return err
	})
}

func (r *Repository) deleteMetadata(ctx context.Context, q queryer, item store.DeleteItem) error {
	// Include legacy playback rows whose IDs predate the object registry.
	ids := map[string]bool{}
	for _, table := range []string{"media_objects", "media_playback_stats"} {
		where, args := r.pathScope("media_path", item.Path, item.Directory)
		rows, err := q.QueryContext(ctx, "SELECT media_id FROM "+table+" WHERE "+where, args...)
		if err != nil {
			return err
		}
		for rows.Next() {
			var id string
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			ids[id] = true
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
	}
	ordered := make([]string, 0, len(ids))
	for id := range ids {
		ordered = append(ordered, id)
	}
	sort.Strings(ordered)
	for _, id := range ordered {
		if _, err := q.ExecContext(ctx, "DELETE FROM media_playback_events WHERE media_id=?", id); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "DELETE FROM media_playback_stats WHERE media_id=?", id); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "DELETE FROM media_lyric_links WHERE media_id=? OR lyric_id=?", id, id); err != nil {
			return err
		}
	}
	for _, column := range []string{"media_path", "lyric_path"} {
		where, args := r.pathScope(column, item.Path, item.Directory)
		if _, err := q.ExecContext(ctx, "DELETE FROM media_lyric_links WHERE "+where, args...); err != nil {
			return err
		}
	}
	if item.Directory {
		where, args := r.pathScope("relative_path", item.Path, true)
		if _, err := q.ExecContext(ctx, "DELETE FROM media_visibility WHERE "+where, args...); err != nil {
			return err
		}
	}
	where, args := r.pathScope("media_path", item.Path, item.Directory)
	_, err := q.ExecContext(ctx, "DELETE FROM media_objects WHERE "+where, args...)
	return err
}

func (r *Repository) CommitDelete(ctx context.Context, id string, audit store.AdminAudit) error {
	return r.write(ctx, func(q queryer) error {
		operation, err := r.deleteOperation(ctx, q, id, true)
		if err != nil {
			return err
		}
		if operation == nil {
			return errors.New("delete operation missing")
		}
		if operation.State == "committed" {
			return nil
		}
		if operation.State != "pending" {
			return errors.New("invalid delete operation state")
		}
		for _, item := range operation.Items {
			if err := r.deleteMetadata(ctx, q, item); err != nil {
				return err
			}
		}
		if _, err := q.ExecContext(ctx, "UPDATE media_delete_operations SET state='committed' WHERE operation_id=?", id); err != nil {
			return err
		}
		audit.Result = "success"
		detail, _ := json.Marshal(map[string]any{"operation_id": id})
		audit.Detail = string(detail)
		return appendMutationAudit(ctx, q, audit, deletePaths(operation.Items))
	})
}
