package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (r *Repository) checkOwnedRename(ctx context.Context, q queryer, relationship, old, target string) error {
	if !nodeIDPattern.MatchString(relationship) || !store.ValidDirectoryRename(old, target) {
		return nodeConflict("invalid owned directory rename")
	}
	if _, err := r.ownedStorageNode(ctx, q, relationship); err != nil {
		return err
	}
	for _, name := range []string{old, target} {
		where, args := r.pathScope("media_path", name, true)
		var count int
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM cluster_upload_sessions WHERE state='reserved' AND "+where, args...).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return nodeConflict("directory has reserved uploads")
		}
	}
	rows, err := q.QueryContext(ctx, "SELECT manifest FROM media_delete_operations WHERE state='pending'"+r.lock())
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return err
		}
		var items []store.DeleteItem
		if err := json.Unmarshal(raw, &items); err != nil {
			return err
		}
		for _, item := range items {
			for _, name := range []string{old, target} {
				if item.Path == name || strings.HasPrefix(item.Path, name+"/") || item.Directory && strings.HasPrefix(name, item.Path+"/") {
					return nodeConflict("directory has unresolved deletion")
				}
			}
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	rows.Close()
	return r.checkRename(ctx, q, target)
}

func (r *Repository) CheckOwnedRename(ctx context.Context, relationship, old, target string) error {
	return r.write(ctx, func(q queryer) error { return r.checkOwnedRename(ctx, q, relationship, old, target) })
}

func (r *Repository) ownedRenameCompleted(ctx context.Context, q queryer, relationship, old, target, operation string) (bool, error) {
	if !nodeIDPattern.MatchString(operation) || !nodeIDPattern.MatchString(relationship) || !store.ValidDirectoryRename(old, target) {
		return false, nodeConflict("invalid owned directory rename")
	}
	// Include the relationship and exact case-sensitive paths in the durable
	// marker. An unrelated rename's audit cannot prove this move completed.
	var source string
	needle := `"operation_id":"` + operation + `"`
	err := q.QueryRowContext(ctx, "SELECT source_summary FROM admin_audit_log WHERE action='directory_rename' AND result='success' AND source_summary LIKE ? ESCAPE '!' LIMIT 1"+r.lock(), "%"+escapeLike(needle)+"%").Scan(&source)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if source != renameSource(old, target, operation, relationship) {
		return false, nodeConflict("rename operation binding differs")
	}
	return true, nil
}

func (r *Repository) OwnedRenameCompleted(ctx context.Context, relationship, old, target, operation string) (done bool, err error) {
	err = r.write(ctx, func(q queryer) error {
		if _, e := r.ownedStorageNode(ctx, q, relationship); e != nil {
			return e
		}
		done, err = r.ownedRenameCompleted(ctx, q, relationship, old, target, operation)
		return err
	})
	return
}

func (r *Repository) CompleteOwnedRename(ctx context.Context, relationship, old, target, operation string, a store.NodeAudit) error {
	return r.write(ctx, func(q queryer) error {
		if _, err := r.ownedStorageNode(ctx, q, relationship); err != nil {
			return err
		}
		done, err := r.ownedRenameCompleted(ctx, q, relationship, old, target, operation)
		if err != nil || done {
			return err
		}
		if err := r.checkOwnedRename(ctx, q, relationship, old, target); err != nil {
			return err
		}
		if err := r.completeRename(ctx, q, old, target, operation, relationship, store.AdminAudit{RequestID: a.RequestID, TraceID: a.TraceID}); err != nil {
			return err
		}
		// Completed Follower publications remain the source of exact-size deletion
		// accounting. Update their paths, not IDs/bytes/state or member balances.
		where, args := r.pathScope("media_path", old, true)
		rows, err := q.QueryContext(ctx, "SELECT upload_id,media_path FROM cluster_upload_sessions WHERE state='complete' AND "+where+" ORDER BY upload_id"+r.lock(), args...)
		if err != nil {
			return err
		}
		type entry struct{ id, name string }
		values := []entry{}
		for rows.Next() {
			var value entry
			if err := rows.Scan(&value.id, &value.name); err != nil {
				rows.Close()
				return err
			}
			values = append(values, value)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for _, value := range values {
			if _, err := q.ExecContext(ctx, "UPDATE cluster_upload_sessions SET media_path=?,updated_at=? WHERE upload_id=?", target+strings.TrimPrefix(value.name, old), time.Now().Unix(), value.id); err != nil {
				return err
			}
		}
		return r.nodeAudit(ctx, q, "storage-directory-renamed", relationship, map[string]any{"operation_id": operation, "old_path": old, "new_path": target}, a)
	})
}
