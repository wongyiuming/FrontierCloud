package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (r *Repository) globalStatsPath(ctx context.Context, q queryer, id string) (string, error) {
	node, err := readNode(ctx, q, "")
	if err != nil {
		return "", err
	}
	if node.Role != "Master" {
		return "", nodeConflict("only Master owns global playback accounting")
	}
	var path string
	err = q.QueryRowContext(ctx, "SELECT media_path FROM global_media_objects WHERE media_id=? AND state='active'"+r.shareLock(), id).Scan(&path)
	if errors.Is(err, sql.ErrNoRows) {
		err = nodeConflict("global media object no longer active")
	}
	return path, err
}
func (r *Repository) RecordGlobalPlayback(ctx context.Context, id, session string) (result store.PlaybackResult, err error) {
	if !nodeHashPattern.MatchString(id) {
		return result, nodeConflict("invalid global media identity")
	}
	err = r.write(ctx, func(q queryer) error {
		path, err := r.globalStatsPath(ctx, q, id)
		if err != nil {
			return err
		}
		return r.recordPlayback(ctx, q, id, path, session, &result)
	})
	return
}
func (r *Repository) SetGlobalPreference(ctx context.Context, id string, value int, a store.AdminAudit) (result store.PlaybackResult, err error) {
	if !nodeHashPattern.MatchString(id) || value < -7 || value > 500 {
		return result, nodeConflict("invalid global media preference")
	}
	err = r.write(ctx, func(q queryer) error {
		result = store.PlaybackResult{MediaID: id}
		path, err := r.globalStatsPath(ctx, q, id)
		if err != nil {
			return err
		}
		now := timestamp(time.Now())
		query := "INSERT INTO media_playback_stats(media_id,media_path,play_score,preference,created_at,updated_at) VALUES (?,?,0,?,?,?)"
		if r.backend == "sqlite" {
			query += " ON CONFLICT(media_id) DO UPDATE SET media_path=excluded.media_path,preference=excluded.preference,updated_at=excluded.updated_at"
		} else {
			query += " ON DUPLICATE KEY UPDATE media_path=VALUES(media_path),preference=VALUES(preference),updated_at=VALUES(updated_at)"
		}
		if _, err = q.ExecContext(ctx, query, id, path, value, now, now); err != nil {
			return err
		}
		if err = q.QueryRowContext(ctx, "SELECT play_score,preference FROM media_playback_stats WHERE media_id=?"+r.lock(), id).Scan(&result.PlayScore, &result.Preference); err != nil {
			return err
		}
		detail, _ := json.Marshal(map[string]any{"resource_id": id, "play_score": result.PlayScore, "preference": value})
		a.Detail = string(detail)
		a.Result = "success"
		a.TargetCount = 1
		return appendMutationAudit(ctx, q, a, []string{path})
	})
	return
}
