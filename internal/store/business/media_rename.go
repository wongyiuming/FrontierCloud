package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (r *Repository) checkRename(ctx context.Context, q queryer, target string) error {
	where, args := r.pathScope("media_path", target, true)
	var count int
	if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM media_objects WHERE object_kind<>'directory' AND "+where, args...).Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return os.ErrExist
	}
	return nil
}
func (r *Repository) CheckRename(ctx context.Context, target string) error {
	return r.checkRename(ctx, r.db, target)
}

// The filesystem journal is replayed before the server starts. Paths change,
// identities and playback event keys do not. The success audit is an atomic
// idempotency marker, including when a commit acknowledgement was lost.
func (r *Repository) CompleteRename(ctx context.Context, old, target, operation string, audit store.AdminAudit) error {
	return r.write(ctx, func(q queryer) error {
		source, _ := json.Marshal(map[string]string{"operation_id": operation, "old_path": old, "new_path": target})
		var found int
		err := q.QueryRowContext(ctx, "SELECT 1 FROM admin_audit_log WHERE action='directory_rename' AND source_summary=? AND result='success' LIMIT 1"+r.lock(), string(source)).Scan(&found)
		if err == nil {
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if err := r.checkRename(ctx, q, target); err != nil {
			return err
		}
		// Only stale directory identities may be reused. A non-directory target
		// remains a conflict even if its backing file is currently absent.
		if err := r.deleteMetadata(ctx, q, store.DeleteItem{Path: target, Directory: true}); err != nil {
			return err
		}
		now := timestamp(time.Now())
		for _, table := range []string{"media_objects", "media_playback_stats", "media_lyric_links"} {
			where, args := r.pathScope("media_path", old, true)
			rows, err := q.QueryContext(ctx, "SELECT media_id,media_path FROM "+table+" WHERE "+where+" ORDER BY media_id"+r.lock(), args...)
			if err != nil {
				return err
			}
			type row struct{ id, path string }
			values := []row{}
			for rows.Next() {
				var v row
				if err := rows.Scan(&v.id, &v.path); err != nil {
					rows.Close()
					return err
				}
				values = append(values, v)
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
			for _, v := range values {
				name := target + strings.TrimPrefix(v.path, old)
				query := "UPDATE " + table + " SET media_path=?,updated_at=? WHERE media_id=?"
				parameters := []any{name, now, v.id}
				if table == "media_objects" {
					query = "UPDATE media_objects SET media_path=?,path_locator=?,updated_at=? WHERE media_id=?"
					parameters = []any{name, locator(name), now, v.id}
				}
				if _, err := q.ExecContext(ctx, query, parameters...); err != nil {
					return err
				}
			}
		}
		where, args := r.pathScope("relative_path", old, true)
		rows, err := q.QueryContext(ctx, "SELECT relative_path FROM media_visibility WHERE "+where+r.lock(), args...)
		if err != nil {
			return err
		}
		paths := []string{}
		for rows.Next() {
			var name string
			if err := rows.Scan(&name); err != nil {
				rows.Close()
				return err
			}
			paths = append(paths, name)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for _, name := range paths {
			where, args := r.pathScope("relative_path", name, false)
			args = append([]any{target + strings.TrimPrefix(name, old), now}, args...)
			if _, err := q.ExecContext(ctx, "UPDATE media_visibility SET relative_path=?,updated_at=? WHERE "+where, args...); err != nil {
				return err
			}
		}
		audit.Action = "directory_rename"
		audit.Result = "success"
		audit.TargetCount = 1
		audit.SourceSummary = string(source)
		audit.Detail = string(source)
		return appendAudit(ctx, q, audit)
	})
}
