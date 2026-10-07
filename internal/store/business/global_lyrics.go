package business

import (
	"context"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// Global tracks are placements, not local files. Never manufacture a local
// media_objects row for a remote track when repairing its default lyric link.
func (r *Repository) BindGlobalLyric(ctx context.Context, id string, lyric store.MediaObject) error {
	if !nodeHashPattern.MatchString(id) || lyric.Kind != "lyric" || lyric.Path != "lyrics/default.lrc" {
		return nodeConflict("invalid global lyric repair")
	}
	return r.write(ctx, func(q queryer) error {
		path, err := r.globalStatsPath(ctx, q, id)
		if err != nil {
			return err
		}
		var kind string
		if err = q.QueryRowContext(ctx, "SELECT object_kind FROM global_media_objects WHERE media_id=? AND state='active'"+r.shareLock(), id).Scan(&kind); err != nil {
			return err
		}
		if kind != "audio" {
			return nodeConflict("global object is not audio")
		}
		lyricID, err := r.ensureWithLock(ctx, q, lyric, r.lock())
		if err != nil {
			return err
		}
		return r.upsertLyric(ctx, q, id, path, lyricID, lyric.Path)
	})
}
