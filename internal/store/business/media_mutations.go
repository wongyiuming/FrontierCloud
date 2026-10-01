package business

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (r *Repository) SetPreference(ctx context.Context, o store.MediaObject, value int, audit store.AdminAudit) (result store.PlaybackResult, err error) {
	if value < -7 || value > 500 {
		return result, errors.New("Preference must be between -7 and 500")
	}
	err = r.write(ctx, func(q queryer) error {
		result = store.PlaybackResult{}
		id, err := r.ensure(ctx, q, o)
		if err != nil {
			return err
		}
		result.MediaID = id
		now := timestamp(time.Now())
		query := "INSERT INTO media_playback_stats (media_id,media_path,play_score,preference,created_at,updated_at) VALUES (?,?,0,?,?,?)"
		if r.backend == "sqlite" {
			query += " ON CONFLICT(media_id) DO UPDATE SET media_path=excluded.media_path,preference=excluded.preference,updated_at=excluded.updated_at"
		} else {
			query += " ON DUPLICATE KEY UPDATE media_path=VALUES(media_path),preference=VALUES(preference),updated_at=VALUES(updated_at)"
		}
		if _, err := q.ExecContext(ctx, query, id, o.Path, value, now, now); err != nil {
			return err
		}
		if err := q.QueryRowContext(ctx, "SELECT play_score,preference FROM media_playback_stats WHERE media_id=?"+r.lock(), id).Scan(&result.PlayScore, &result.Preference); err != nil {
			return err
		}
		detail, _ := json.Marshal(map[string]any{"media_id": id, "preference": result.Preference, "play_score": result.PlayScore})
		audit.Detail = string(detail)
		audit.Result = "success"
		audit.TargetCount = 1
		return appendMutationAudit(ctx, q, audit, []string{o.Path})
	})
	return
}

func escapeLike(value string) string {
	value = strings.ReplaceAll(value, "!", "!!")
	value = strings.ReplaceAll(value, "%", "!%")
	return strings.ReplaceAll(value, "_", "!_")
}

func (r *Repository) SetHidden(ctx context.Context, paths []string, hide bool, audit store.AdminAudit) error {
	return r.write(ctx, func(q queryer) error {
		for _, path := range paths {
			if hide {
				query := "INSERT INTO media_visibility (relative_path,hidden,updated_at) VALUES (?,1,?)"
				if r.backend == "sqlite" {
					query += " ON CONFLICT(relative_path) DO UPDATE SET hidden=1,updated_at=excluded.updated_at"
				} else {
					query += " ON DUPLICATE KEY UPDATE hidden=1,updated_at=VALUES(updated_at)"
				}
				if _, err := q.ExecContext(ctx, query, path, timestamp(time.Now())); err != nil {
					return err
				}
			} else {
				// Revealing a parent also clears inherited descendant hides. Escape
				// LIKE metacharacters and use binary path comparison on MySQL.
				where, args := r.pathScope("relative_path", path, true)
				if _, err := q.ExecContext(ctx, "DELETE FROM media_visibility WHERE "+where, args...); err != nil {
					return err
				}
			}
		}
		audit.Result = "success"
		audit.TargetCount = len(paths)
		detail, _ := json.Marshal(map[string]any{"hidden": hide})
		audit.Detail = string(detail)
		return appendMutationAudit(ctx, q, audit, paths)
	})
}
