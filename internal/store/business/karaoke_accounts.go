package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

const userColumns = "user_id,username,username_key,password_hash,status,quota_bytes,used_bytes,created_at,updated_at"

var registrationDay = regexp.MustCompile(`^[0-9]{8}$`)

func scanUser(row rowScanner) (v store.KaraokeUser, err error) {
	err = row.Scan(&v.ID, &v.Username, &v.NameKey, &v.PasswordHash, &v.Status, &v.Quota, &v.Used, &v.CreatedAt, &v.UpdatedAt)
	return
}
func userLookup(row rowScanner) (*store.KaraokeUser, error) {
	v, err := scanUser(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &v, nil
}
func (r *Repository) UserByID(ctx context.Context, id string) (*store.KaraokeUser, error) {
	if !nodeIDPattern.MatchString(id) {
		return nil, nil
	}
	return userLookup(r.db.QueryRowContext(ctx, "SELECT "+userColumns+" FROM karaoke_users WHERE user_id=?", id))
}
func (r *Repository) UserByName(ctx context.Context, key string) (*store.KaraokeUser, error) {
	return userLookup(r.db.QueryRowContext(ctx, "SELECT "+userColumns+" FROM karaoke_users WHERE username_key=?", key))
}
func (r *Repository) accountMaster(ctx context.Context, q queryer) error {
	n, err := readNode(ctx, q, r.lock())
	if err != nil {
		return err
	}
	if n.Role != "Master" {
		return nodeConflict("accounts require a Master")
	}
	return nil
}
func (r *Repository) karaokeAudit(ctx context.Context, q queryer, a store.KaraokeAudit) error {
	if len(a.Action) > 48 || len(a.Result) > 24 || len(a.IP) > 45 || len(a.RequestID) > 128 || len(a.TraceID) > 32 {
		return errors.New("invalid account audit context")
	}
	if a.Addresses == nil {
		a.Addresses = []string{}
	}
	if a.Detail == nil {
		a.Detail = map[string]any{}
	}
	addresses, err := json.Marshal(a.Addresses)
	if err != nil {
		return err
	}
	detail, err := json.Marshal(a.Detail)
	if err != nil {
		return err
	}
	var user any
	if a.UserID != "" {
		if !nodeIDPattern.MatchString(a.UserID) {
			return store.ErrUserMissing
		}
		user = a.UserID
	}
	_, err = q.ExecContext(ctx, "INSERT INTO karaoke_audit_log(user_id,action,result,client_ip,webrtc_addresses,detail,request_id,trace_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)", user, a.Action, a.Result, a.IP, string(addresses), string(detail), a.RequestID, a.TraceID, time.Now().Unix())
	return err
}
func (r *Repository) AccountAudit(ctx context.Context, a store.KaraokeAudit) error {
	return r.write(ctx, func(q queryer) error {
		if err := r.accountMaster(ctx, q); err != nil {
			return err
		}
		return r.karaokeAudit(ctx, q, a)
	})
}
func (r *Repository) daily(ctx context.Context, q queryer, ip, day string, lock bool) (v store.RegistrationCounts, err error) {
	query := "SELECT failure_count,success_count FROM karaoke_registration_daily WHERE client_ip=? AND day_key=?"
	if lock {
		query += r.lock()
	}
	err = q.QueryRowContext(ctx, query, ip, day).Scan(&v.Failures, &v.Successes)
	if errors.Is(err, sql.ErrNoRows) && !lock {
		err = nil
	}
	return
}
func (r *Repository) RegistrationCounts(ctx context.Context, ip, day string) (store.RegistrationCounts, error) {
	return r.daily(ctx, r.db, ip, day, false)
}
func (r *Repository) initializeDaily(ctx context.Context, q queryer, ip, day string) error {
	if len(ip) == 0 || len(ip) > 45 || !registrationDay.MatchString(day) {
		return errors.New("invalid daily registration key")
	}
	_, err := q.ExecContext(ctx, r.ignoreInsert()+" INTO karaoke_registration_daily(client_ip,day_key,failure_count,success_count,updated_at) VALUES (?,?,0,0,?)", ip, day, time.Now().Unix())
	return err
}
func (r *Repository) RegistrationFailure(ctx context.Context, ip, day string, a store.KaraokeAudit) error {
	return r.write(ctx, func(q queryer) error {
		if err := r.accountMaster(ctx, q); err != nil {
			return err
		}
		if err := r.initializeDaily(ctx, q, ip, day); err != nil {
			return err
		}
		v, err := r.daily(ctx, q, ip, day, true)
		if err != nil {
			return err
		}
		if v.Failures < 20 {
			if _, err := q.ExecContext(ctx, "UPDATE karaoke_registration_daily SET failure_count=failure_count+1,updated_at=? WHERE client_ip=? AND day_key=?", time.Now().Unix(), ip, day); err != nil {
				return err
			}
		}
		a.Action, a.Result, a.IP = "register", "failure", ip
		return r.karaokeAudit(ctx, q, a)
	})
}
func (r *Repository) RegisterUser(ctx context.Context, v store.KaraokeUser, ip, day string, a store.KaraokeAudit) error {
	if !nodeIDPattern.MatchString(v.ID) || len(v.Username) == 0 || len(v.NameKey) == 0 || v.PasswordHash == "" || v.Quota < 1 || v.Quota > store.MaxStorageAllocation {
		return store.ErrAccountConflict
	}
	return r.write(ctx, func(q queryer) error {
		if err := r.accountMaster(ctx, q); err != nil {
			return err
		}
		if err := r.initializeDaily(ctx, q, ip, day); err != nil {
			return err
		}
		counts, err := r.daily(ctx, q, ip, day, true)
		if err != nil {
			return err
		}
		if counts.Failures >= 20 || counts.Successes >= 3 {
			return store.ErrRegistrationLimit
		}
		var count int
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM karaoke_users WHERE username_key=?", v.NameKey).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return store.ErrUserExists
		}
		now := time.Now().Unix()
		if _, err := q.ExecContext(ctx, "INSERT INTO karaoke_users("+userColumns+") VALUES (?,?,?,?,'active',?,0,?,?)", v.ID, v.Username, v.NameKey, v.PasswordHash, v.Quota, now, now); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "UPDATE karaoke_registration_daily SET success_count=success_count+1,updated_at=? WHERE client_ip=? AND day_key=?", now, ip, day); err != nil {
			return err
		}
		a.UserID, a.Action, a.Result, a.IP = v.ID, "register", "success", ip
		return r.karaokeAudit(ctx, q, a)
	})
}
func (r *Repository) ConfirmLogin(ctx context.Context, id, expectedHash string, a store.KaraokeAudit) error {
	return r.write(ctx, func(q queryer) error {
		if err := r.accountMaster(ctx, q); err != nil {
			return err
		}
		v, err := scanUser(q.QueryRowContext(ctx, "SELECT "+userColumns+" FROM karaoke_users WHERE user_id=?"+r.lock(), id))
		if errors.Is(err, sql.ErrNoRows) {
			return store.ErrUserMissing
		}
		if err != nil {
			return err
		}
		if v.Status != "active" {
			return store.ErrUserBlocked
		}
		if v.PasswordHash != expectedHash {
			return store.ErrAccountConflict
		}
		a.UserID, a.Action, a.Result = id, "login", "success"
		return r.karaokeAudit(ctx, q, a)
	})
}
func (r *Repository) ChangePassword(ctx context.Context, id, expectedHash, newHash string, a store.KaraokeAudit) error {
	if newHash == "" {
		return store.ErrAccountConflict
	}
	return r.write(ctx, func(q queryer) error {
		if err := r.accountMaster(ctx, q); err != nil {
			return err
		}
		v, err := scanUser(q.QueryRowContext(ctx, "SELECT "+userColumns+" FROM karaoke_users WHERE user_id=?"+r.lock(), id))
		if errors.Is(err, sql.ErrNoRows) {
			return store.ErrUserMissing
		}
		if err != nil {
			return err
		}
		if v.Status != "active" {
			return store.ErrUserBlocked
		}
		if v.PasswordHash != expectedHash {
			return store.ErrAccountConflict
		}
		if _, err := q.ExecContext(ctx, "UPDATE karaoke_users SET password_hash=?,updated_at=? WHERE user_id=?", newHash, time.Now().Unix(), id); err != nil {
			return err
		}
		a.UserID, a.Action, a.Result = id, "password-change", "success"
		return r.karaokeAudit(ctx, q, a)
	})
}
func (r *Repository) ListUsers(ctx context.Context, query string, page, size int) (result store.UserPage, err error) {
	n, err := r.ReadIdentity(ctx)
	if err != nil {
		return result, err
	}
	if n.Role != "Master" {
		return result, store.ErrNodeState
	}
	if page < 1 {
		page = 1
	}
	size = min(100, max(1, size))
	where, args := "", []any{}
	query = strings.TrimSpace(query)
	if query != "" {
		where = " WHERE username LIKE ? ESCAPE '!'"
		args = append(args, "%"+escapeLike(query)+"%")
	}
	if err := r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM karaoke_users"+where, args...).Scan(&result.Total); err != nil {
		return result, err
	}
	result.Page, result.Pages, result.Items = page, max(1, (result.Total+size-1)/size), []store.KaraokeUser{}
	// Avoid integer multiplication overflow from an untrusted page parameter.
	if page > result.Pages {
		return result, nil
	}
	rows, err := r.db.QueryContext(ctx, "SELECT "+userColumns+" FROM karaoke_users"+where+" ORDER BY created_at DESC,user_id LIMIT ? OFFSET ?", append(args, size, (page-1)*size)...)
	if err != nil {
		return result, err
	}
	defer rows.Close()
	for rows.Next() {
		v, e := scanUser(rows)
		if e != nil {
			return result, e
		}
		result.Items = append(result.Items, v)
	}
	return result, rows.Err()
}
func (r *Repository) MutateUser(ctx context.Context, id, action string, quota int64, a store.KaraokeAudit) error {
	if action != "ban" && action != "unban" && action != "quota" {
		return store.ErrAccountConflict
	}
	return r.write(ctx, func(q queryer) error {
		if err := r.accountMaster(ctx, q); err != nil {
			return err
		}
		v, err := scanUser(q.QueryRowContext(ctx, "SELECT "+userColumns+" FROM karaoke_users WHERE user_id=?"+r.lock(), id))
		if errors.Is(err, sql.ErrNoRows) {
			return store.ErrUserMissing
		}
		if err != nil {
			return err
		}
		if v.Status != "active" && v.Status != "banned" {
			return store.ErrAccountConflict
		}
		now := time.Now().Unix()
		if action == "quota" {
			if quota < v.Used || quota < 1024*1024 || quota > store.MaxStorageAllocation {
				return store.ErrUserQuota
			}
			_, err = q.ExecContext(ctx, "UPDATE karaoke_users SET quota_bytes=?,updated_at=? WHERE user_id=?", quota, now, id)
		} else {
			status := "active"
			if action == "ban" {
				status = "banned"
			}
			_, err = q.ExecContext(ctx, "UPDATE karaoke_users SET status=?,updated_at=? WHERE user_id=?", status, now, id)
		}
		if err != nil {
			return err
		}
		a.UserID, a.Action, a.Result = id, "admin-user-"+action, "success"
		if a.Detail == nil {
			a.Detail = map[string]any{}
		}
		a.Detail["quota_bytes"] = quota
		return r.karaokeAudit(ctx, q, a)
	})
}

var _ store.KaraokeRepository = (*Repository)(nil)
