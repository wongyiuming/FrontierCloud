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

func (r *Repository) upsertLyric(ctx context.Context, q queryer, trackID, trackPath, lyricID, lyricPath string) error {
	now := timestamp(time.Now())
	query := "INSERT INTO media_lyric_links (media_id,media_path,lyric_id,lyric_path,created_at,updated_at) VALUES (?,?,?,?,?,?)"
	if r.backend == "sqlite" {
		query += " ON CONFLICT(media_id) DO UPDATE SET media_path=excluded.media_path,lyric_id=excluded.lyric_id,lyric_path=excluded.lyric_path,updated_at=excluded.updated_at"
	} else {
		query += " ON DUPLICATE KEY UPDATE media_path=VALUES(media_path),lyric_id=VALUES(lyric_id),lyric_path=VALUES(lyric_path),updated_at=VALUES(updated_at)"
	}
	_, err := q.ExecContext(ctx, query, trackID, trackPath, lyricID, lyricPath, now, now)
	return err
}
func (r *Repository) LyricRelations(ctx context.Context, trackScope, lyricScope string) ([]store.LyricRelation, error) {
	trackWhere, trackArgs := r.pathScope("t.media_path", trackScope, true)
	lyricWhere, lyricArgs := r.pathScope("l.media_path", lyricScope, true)
	tracks, active := "media_objects", ""
	node, err := r.ReadIdentity(ctx)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	if node.Role == "Master" {
		tracks, active = "global_media_objects", " AND t.state='active'"
	}
	rows, err := r.db.QueryContext(ctx, "SELECT t.media_path,l.media_path FROM media_lyric_links b JOIN "+tracks+" t ON t.media_id=b.media_id JOIN media_objects l ON l.media_id=b.lyric_id WHERE t.object_kind='audio' AND l.object_kind='lyric'"+active+" AND ("+trackWhere+" OR "+lyricWhere+") ORDER BY t.media_path", append(trackArgs, lyricArgs...)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []store.LyricRelation{}
	for rows.Next() {
		var relation store.LyricRelation
		if err := rows.Scan(&relation.Track, &relation.Lyric); err != nil {
			return nil, err
		}
		result = append(result, relation)
	}
	return result, rows.Err()
}
func (r *Repository) lockLyricObjects(ctx context.Context, q queryer, objects []store.MediaObject) (map[string]string, error) {
	node, err := readNode(ctx, q, "")
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	unique := map[string]store.MediaObject{}
	for _, object := range objects {
		if object.Kind != "audio" && object.Kind != "lyric" {
			return nil, errors.New("invalid lyric relation object")
		}
		if prior, ok := unique[object.Path]; ok && prior.Kind != object.Kind {
			return nil, errors.New("lyric object kind conflict")
		}
		unique[object.Path] = object
	}
	paths := make([]string, 0, len(unique))
	for p := range unique {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	ids := map[string]string{}
	for _, p := range paths {
		if node.Role == "Master" && unique[p].Kind == "audio" {
			var id string
			if err := q.QueryRowContext(ctx, "SELECT media_id FROM global_media_objects WHERE path_locator=? AND media_path=? AND object_kind='audio' AND state='active'"+r.lock(), locator(p), p).Scan(&id); err != nil {
				return nil, err
			}
			if unique[p].ID != "" && unique[p].ID != id {
				return nil, nodeConflict("global lyric track identity mismatch")
			}
			ids[p] = id
			continue
		}
		id, err := r.ensureWithLock(ctx, q, unique[p], r.lock())
		if err != nil {
			return nil, err
		}
		ids[p] = id
	}
	return ids, nil
}
func (r *Repository) ReplaceLyricRelations(ctx context.Context, origin store.MediaObject, targets []store.MediaObject, fallback store.MediaObject, audit store.AdminAudit) (int, error) {
	if fallback.Kind != "lyric" || fallback.Path != "lyrics/default.lrc" || origin.Kind == "lyric" && origin.Path == fallback.Path {
		return 0, errors.New("invalid system lyric mutation")
	}
	count := 0
	err := r.write(ctx, func(q queryer) error {
		objects := append([]store.MediaObject{origin, fallback}, targets...)
		ids, err := r.lockLyricObjects(ctx, q, objects)
		if err != nil {
			return err
		}
		if origin.Kind == "audio" {
			if len(targets) > 1 {
				return errors.New("one track may bind at most one lyric")
			}
			lyric := fallback
			if len(targets) == 1 {
				lyric = targets[0]
			}
			if lyric.Kind != "lyric" {
				return errors.New("invalid lyric target")
			}
			if err := r.upsertLyric(ctx, q, ids[origin.Path], origin.Path, ids[lyric.Path], lyric.Path); err != nil {
				return err
			}
			count = 1
		} else {
			rows, err := q.QueryContext(ctx, "SELECT media_id,media_path FROM media_lyric_links WHERE lyric_id=? ORDER BY media_id"+r.lock(), ids[origin.Path])
			if err != nil {
				return err
			}
			type row struct{ id, path string }
			previous := []row{}
			for rows.Next() {
				var value row
				if err := rows.Scan(&value.id, &value.path); err != nil {
					rows.Close()
					return err
				}
				previous = append(previous, value)
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
			for _, v := range previous {
				if err := r.upsertLyric(ctx, q, v.id, v.path, ids[fallback.Path], fallback.Path); err != nil {
					return err
				}
			}
			for _, track := range targets {
				if track.Kind != "audio" {
					return errors.New("invalid track target")
				}
				if err := r.upsertLyric(ctx, q, ids[track.Path], track.Path, ids[origin.Path], origin.Path); err != nil {
					return err
				}
			}
			count = len(targets)
		}
		paths := []string{origin.Path}
		for _, target := range targets {
			paths = append(paths, target.Path)
		}
		audit.Action = "lyric_relations"
		audit.Result = "success"
		detail, _ := json.Marshal(map[string]any{"origin_path": origin.Path, "relations": count})
		audit.Detail = string(detail)
		return appendMutationAudit(ctx, q, audit, paths)
	})
	return count, err
}
func (r *Repository) AutoLyricRelations(ctx context.Context, pairs []store.LyricPair, result store.AutoLyricResult, audit store.AdminAudit) (store.AutoLyricResult, error) {
	err := r.write(ctx, func(q queryer) error {
		result.Linked = 0
		result.Preserved = 0
		objects := []store.MediaObject{}
		for _, pair := range pairs {
			if pair.Track.Kind != "audio" || pair.Lyric.Kind != "lyric" {
				return errors.New("invalid automatic lyric pair")
			}
			objects = append(objects, pair.Track, pair.Lyric)
		}
		ids, err := r.lockLyricObjects(ctx, q, objects)
		if err != nil {
			return err
		}
		linked := []string{}
		for _, pair := range pairs {
			var current string
			err := q.QueryRowContext(ctx, "SELECT lyric_path FROM media_lyric_links WHERE media_id=?"+r.lock(), ids[pair.Track.Path]).Scan(&current)
			if err != nil && !errors.Is(err, sql.ErrNoRows) {
				return err
			}
			// Inspect stored choice, not file availability or an optional JOIN.
			if current != "" && current != "lyrics/default.lrc" {
				result.Preserved++
				continue
			}
			if err := r.upsertLyric(ctx, q, ids[pair.Track.Path], pair.Track.Path, ids[pair.Lyric.Path], pair.Lyric.Path); err != nil {
				return err
			}
			result.Linked++
			linked = append(linked, pair.Track.Path)
		}
		audit.Action = "lyric_auto_relate"
		audit.Result = "success"
		detail, _ := json.Marshal(result)
		audit.Detail = string(detail)
		return appendMutationAudit(ctx, q, audit, linked)
	})
	return result, err
}
