// Package business implements domain repositories over the selected SQL driver.
// Only this persistence layer chooses SQLite/MySQL SQL variants.
package business

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	driver "github.com/go-sql-driver/mysql"
	"sort"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type Repository struct {
	db      *sql.DB
	backend string
}

func New(db *sql.DB, backend string) *Repository { return &Repository{db, backend} }

type queryer interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
	ExecContext(context.Context, string, ...any) (sql.Result, error)
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

// SQLite reserves the write lock BEFORE any reads; MySQL uses row locks. The
// connection is retained until commit/rollback so pooled connections never see
// a transaction created on another connection.
func (r *Repository) write(ctx context.Context, fn func(queryer) error) error {
	if r.backend != "sqlite" {
		// Only database-only callbacks may enter this retry boundary. Retry a
		// fully rolled-back deadlock, never a network/ambiguous commit failure.
		for attempt := 0; ; attempt++ {
			err := func() error {
				tx, err := r.db.BeginTx(ctx, nil)
				if err != nil {
					return err
				}
				defer tx.Rollback()
				if err := fn(tx); err != nil {
					return err
				}
				return tx.Commit()
			}()
			var mysqlError *driver.MySQLError
			if err == nil || attempt >= 5 || !errors.As(err, &mysqlError) || (mysqlError.Number != 1213 && mysqlError.Number != 1205) {
				return err
			}
			jitter := make([]byte, 1)
			if _, err := rand.Read(jitter); err != nil {
				return err
			}
			timer := time.NewTimer(time.Duration((5<<attempt)+int(jitter[0])%10) * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case <-timer.C:
			}
		}
	}
	conn, err := r.db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	if _, err = conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return err
	}
	defer func() {
		rollback, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, _ = conn.ExecContext(rollback, "ROLLBACK")
	}()
	if err = fn(conn); err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, "COMMIT")
	return err
}

func randomID() (string, error) {
	b := make([]byte, 32)
	_, err := rand.Read(b)
	return hex.EncodeToString(b), err
}
func locator(path string) string {
	sum := sha256.Sum256([]byte(path))
	return hex.EncodeToString(sum[:])
}
func timestamp(t time.Time) string { return t.UTC().Format("2006-01-02 15:04:05.000000") }
func (r *Repository) ignoreInsert() string {
	if r.backend == "sqlite" {
		return "INSERT OR IGNORE"
	}
	return "INSERT IGNORE"
}
func (r *Repository) lock() string {
	if r.backend == "mysql" {
		return " FOR UPDATE"
	}
	return ""
}

func (r *Repository) shareLock() string {
	if r.backend == "mysql" {
		return " FOR SHARE"
	}
	return ""
}

func normalize(path string) (string, error) {
	path = strings.TrimLeft(strings.TrimSpace(strings.ReplaceAll(path, "\\", "/")), "/")
	if path == "" || strings.ContainsRune(path, 0) {
		return "", errors.New("Invalid media object path")
	}
	for _, p := range strings.Split(path, "/") {
		if p == ".." {
			return "", errors.New("Invalid media object path")
		}
	}
	return path, nil
}

func (r *Repository) ensure(ctx context.Context, q queryer, o store.MediaObject) (string, error) {
	return r.ensureWithLock(ctx, q, o, "")
}

func (r *Repository) ensureWithLock(ctx context.Context, q queryer, o store.MediaObject, lock string) (string, error) {
	path, err := normalize(o.Path)
	if err != nil {
		return "", err
	}
	if o.Kind != "audio" && o.Kind != "video" && o.Kind != "lyric" && o.Kind != "directory" {
		return "", errors.New("Invalid media object kind")
	}
	key := locator(path)
	var id, existingPath, kind string
	err = q.QueryRowContext(ctx, "SELECT media_id, media_path, object_kind FROM media_objects WHERE path_locator=?"+lock, key).Scan(&id, &existingPath, &kind)
	if err == nil {
		if path != existingPath || kind != o.Kind {
			return "", errors.New("media path identity conflict")
		}
		return id, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return "", err
	}
	var legacy bool
	query := "SELECT EXISTS(SELECT 1 FROM media_lyric_links WHERE lyric_id=?)"
	args := []any{key}
	if o.Kind != "lyric" {
		query = "SELECT (EXISTS(SELECT 1 FROM media_playback_stats WHERE media_id=?) OR EXISTS(SELECT 1 FROM media_playback_events WHERE media_id=?) OR EXISTS(SELECT 1 FROM media_lyric_links WHERE media_id=?))"
		args = []any{key, key, key}
	}
	if err := q.QueryRowContext(ctx, query, args...).Scan(&legacy); err != nil {
		return "", err
	}
	id = key
	if !legacy {
		id, err = randomID()
		if err != nil {
			return "", err
		}
	}
	now := timestamp(time.Now())
	_, err = q.ExecContext(ctx, r.ignoreInsert()+" INTO media_objects (media_id,object_kind,media_path,path_locator,created_at,updated_at) VALUES (?,?,?,?,?,?)", id, o.Kind, path, key, now, now)
	if err != nil {
		return "", err
	}
	if lock == "" {
		lock = r.shareLock()
	}
	err = q.QueryRowContext(ctx, "SELECT media_id,media_path,object_kind FROM media_objects WHERE path_locator=?"+lock, key).Scan(&id, &existingPath, &kind)
	if err == nil && (path != existingPath || kind != o.Kind) {
		err = errors.New("media path identity conflict")
	}
	return id, err
}

func (r *Repository) EnsureObjects(ctx context.Context, items []store.MediaObject) (map[string]string, error) {
	result := map[string]string{}
	// Deterministic lock order prevents multi-object MySQL deadlocks.
	items = append([]store.MediaObject(nil), items...)
	sort.Slice(items, func(i, j int) bool { return locator(items[i].Path) < locator(items[j].Path) })
	err := r.write(ctx, func(q queryer) error {
		clear(result)
		for _, o := range items {
			id, err := r.ensure(ctx, q, o)
			if err != nil {
				return err
			}
			path, _ := normalize(o.Path)
			result[path] = id
		}
		return nil
	})
	return result, err
}

func (r *Repository) ObjectByID(ctx context.Context, id string) (*store.MediaObject, error) {
	var o store.MediaObject
	err := r.db.QueryRowContext(ctx, "SELECT media_id,media_path,object_kind FROM media_objects WHERE media_id=?", id).Scan(&o.ID, &o.Path, &o.Kind)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return &o, err
}

func (r *Repository) HiddenPaths(ctx context.Context) (map[string]bool, error) {
	result := map[string]bool{}
	rows, err := r.db.QueryContext(ctx, "SELECT relative_path FROM media_visibility WHERE hidden=1")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var path string
		if err := rows.Scan(&path); err != nil {
			return nil, err
		}
		result[path] = true
	}
	return result, rows.Err()
}

func (r *Repository) DirectoryPreferences(ctx context.Context) (map[string]int, error) {
	result := map[string]int{}
	rows, err := r.db.QueryContext(ctx, "SELECT o.media_path,s.preference FROM media_objects o JOIN media_playback_stats s ON o.media_id=s.media_id WHERE o.object_kind='directory'")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var path string
		var value int
		if err := rows.Scan(&path, &value); err != nil {
			return nil, err
		}
		result[path] = value
	}
	return result, rows.Err()
}

func (r *Repository) Stats(ctx context.Context, ids []string) (map[string]store.PlaybackStats, error) {
	result := map[string]store.PlaybackStats{}
	for start := 0; start < len(ids); start += 500 {
		end := min(start+500, len(ids))
		args := make([]any, end-start)
		for i, id := range ids[start:end] {
			args[i] = id
		}
		marks := strings.TrimSuffix(strings.Repeat("?,", len(args)), ",")
		rows, err := r.db.QueryContext(ctx, "SELECT media_id,play_score,preference FROM media_playback_stats WHERE media_id IN ("+marks+")", args...)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var id string
			var s store.PlaybackStats
			if err := rows.Scan(&id, &s.PlayScore, &s.Preference); err != nil {
				rows.Close()
				return nil, err
			}
			result[id] = s
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return nil, err
		}
	}
	return result, nil
}

func (r *Repository) RecordPlayback(ctx context.Context, o store.MediaObject, session string) (result store.PlaybackResult, err error) {
	err = r.write(ctx, func(q queryer) error {
		result = store.PlaybackResult{}
		id, err := r.ensure(ctx, q, o)
		if err != nil {
			return err
		}
		return r.recordPlayback(ctx, q, id, o.Path, session, &result)
	})
	return
}

func (r *Repository) recordPlayback(ctx context.Context, q queryer, id, mediaPath, session string, result *store.PlaybackResult) error {
	*result = store.PlaybackResult{MediaID: id}
	now := time.Now().UTC()
	stamp := timestamp(now)
	query := "INSERT INTO media_playback_stats (media_id,media_path,play_score,preference,created_at,updated_at) VALUES (?,?,0,0,?,?)"
	if r.backend == "sqlite" {
		query += " ON CONFLICT(media_id) DO UPDATE SET media_path=excluded.media_path"
	} else {
		query += " ON DUPLICATE KEY UPDATE media_path=VALUES(media_path)"
	}
	if _, err := q.ExecContext(ctx, query, id, mediaPath, stamp, stamp); err != nil {
		return err
	}
	insert := r.ignoreInsert() + " INTO media_playback_events (playback_session_id,media_id,counted_at,expires_at) VALUES (?,?,?,?)"
	args := []any{session, id, stamp, timestamp(now.Add(7 * 24 * time.Hour))}
	res, err := q.ExecContext(ctx, insert, args...)
	if err != nil {
		return err
	}
	count, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if count != 1 {
		deleted, err := q.ExecContext(ctx, "DELETE FROM media_playback_events WHERE playback_session_id=? AND media_id=? AND expires_at<=?", session, id, stamp)
		if err != nil {
			return err
		}
		removed, err := deleted.RowsAffected()
		if err != nil {
			return err
		}
		if removed == 1 {
			res, err = q.ExecContext(ctx, insert, args...)
			if err != nil {
				return err
			}
			count, err = res.RowsAffected()
			if err != nil {
				return err
			}
		}
	}
	if count == 1 {
		result.Counted = true
		if _, err = q.ExecContext(ctx, "UPDATE media_playback_stats SET play_score=play_score+1, updated_at=? WHERE media_id=?", stamp, id); err != nil {
			return err
		}
	}
	return q.QueryRowContext(ctx, "SELECT play_score,preference FROM media_playback_stats WHERE media_id=?"+r.lock(), id).Scan(&result.PlayScore, &result.Preference)
}

func (r *Repository) LyricPath(ctx context.Context, id string) (string, error) {
	var path string
	err := r.db.QueryRowContext(ctx, "SELECT o.media_path FROM media_lyric_links l JOIN media_objects o ON l.lyric_id=o.media_id WHERE l.media_id=?", id).Scan(&path)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	return path, err
}

func (r *Repository) BindLyric(ctx context.Context, track, lyric store.MediaObject) error {
	return r.write(ctx, func(q queryer) error {
		mediaID, err := r.ensure(ctx, q, track)
		if err != nil {
			return err
		}
		lyricID, err := r.ensure(ctx, q, lyric)
		if err != nil {
			return err
		}
		return r.upsertLyric(ctx, q, mediaID, track.Path, lyricID, lyric.Path)
	})
}

func (r *Repository) InitializeIdentity(ctx context.Context, initial store.NodeIdentity) (result store.NodeIdentity, err error) {
	err = r.write(ctx, func(q queryer) error {
		err := q.QueryRowContext(ctx, "SELECT node_id,`role`,endpoint,private_key,created_at FROM node_identity WHERE singleton=1"+r.lock()).Scan(&result.ID, &result.Role, &result.Endpoint, &result.PrivateKey, &result.CreatedAt)
		if err == nil {
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		_, err = q.ExecContext(ctx, r.ignoreInsert()+" INTO node_identity (singleton,node_id,`role`,endpoint,private_key,created_at) VALUES (1,?,?,?,?,?)", initial.ID, initial.Role, initial.Endpoint, initial.PrivateKey, initial.CreatedAt)
		if err != nil {
			return err
		}
		return q.QueryRowContext(ctx, "SELECT node_id,`role`,endpoint,private_key,created_at FROM node_identity WHERE singleton=1"+r.lock()).Scan(&result.ID, &result.Role, &result.Endpoint, &result.PrivateKey, &result.CreatedAt)
	})
	if err == nil && result.Role != "Standalone" && result.Role != "Master" && result.Role != "Follower" {
		err = fmt.Errorf("unknown node role %q", result.Role)
	}
	return
}
