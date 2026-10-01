package business

import (
	"context"
	"database/sql"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"time"
)

func (r *Repository) StageUserDeletion(ctx context.Context, id string, a store.KaraokeAudit) (found bool, err error) {
	if !nodeIDPattern.MatchString(id) {
		return false, store.ErrUserMissing
	}
	err = r.write(ctx, func(q queryer) error {
		if err := r.accountMaster(ctx, q); err != nil {
			return err
		}
		v, err := scanUser(q.QueryRowContext(ctx, "SELECT "+userColumns+" FROM karaoke_users WHERE user_id=?"+r.lock(), id))
		if errors.Is(err, sql.ErrNoRows) {
			found = false
			return nil
		}
		if err != nil {
			return err
		}
		found = true
		if v.Status == "deleting" {
			return nil
		}
		now := time.Now().Unix()
		if _, err := q.ExecContext(ctx, "UPDATE karaoke_users SET status='deleting',updated_at=? WHERE user_id=?", now, id); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "UPDATE karaoke_recordings SET state='deleting',updated_at=? WHERE user_id=? AND state IN ('pending','ready')", now, id); err != nil {
			return err
		}
		a.UserID, a.Result = id, "pending"
		return r.karaokeAudit(ctx, q, a)
	})
	return
}
func (r *Repository) FinishUserDeletion(ctx context.Context, id string, a store.KaraokeAudit) (complete bool, err error) {
	err = r.write(ctx, func(q queryer) error {
		complete = false
		if err := r.accountMaster(ctx, q); err != nil {
			return err
		}
		v, err := scanUser(q.QueryRowContext(ctx, "SELECT "+userColumns+" FROM karaoke_users WHERE user_id=?"+r.lock(), id))
		if errors.Is(err, sql.ErrNoRows) {
			complete = true
			return nil
		}
		if err != nil {
			return err
		}
		if v.Status != "deleting" {
			return store.ErrAccountConflict
		}
		var count int
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM karaoke_recordings WHERE user_id=?", id).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return nil
		}
		if v.Used != 0 {
			return store.ErrAccountConflict
		}
		a.UserID, a.Result = id, "success"
		if err := r.karaokeAudit(ctx, q, a); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "DELETE FROM karaoke_users WHERE user_id=?", id); err != nil {
			return err
		}
		complete = true
		return nil
	})
	return
}
func (r *Repository) DeletingUsers(ctx context.Context, limit int) ([]string, error) {
	n, err := r.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if n.Role != "Master" {
		return nil, nil
	}
	if limit < 1 || limit > 500 {
		limit = 20
	}
	rows, err := r.db.QueryContext(ctx, "SELECT user_id FROM karaoke_users WHERE status='deleting' ORDER BY updated_at,user_id LIMIT ?", limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	ids := []string{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}
