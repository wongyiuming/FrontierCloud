package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (r *Repository) lockIP(ctx context.Context, q queryer, ip string) error {
	query := "INSERT INTO ip_security_locks(ip_address) VALUES (?)"
	if r.backend == "sqlite" {
		query += " ON CONFLICT(ip_address) DO UPDATE SET ip_address=excluded.ip_address"
	} else {
		query += " ON DUPLICATE KEY UPDATE ip_address=VALUES(ip_address)"
	}
	if _, err := q.ExecContext(ctx, query, ip); err != nil {
		return err
	}
	return nil
}
func (r *Repository) ipAudit(ctx context.Context, q queryer, ip, action string, detail any, a store.AdminAudit) error {
	encoded, err := json.Marshal(detail)
	if err != nil {
		return err
	}
	_, err = q.ExecContext(ctx, "INSERT INTO ip_security_audit_log(ip_address,action,detail,session_id_hash,request_id,trace_id,created_at) VALUES (?,?,?,?,?,?,?)", ip, action, string(encoded), optional(a.SessionHash), optional(bounded(a.RequestID, 128)), optional(bounded(a.TraceID, 32)), timestamp(time.Now()))
	return err
}
func (r *Repository) IPBlock(ctx context.Context, ip string) (*store.IPBan, error) {
	var id int64
	var kind string
	var expiry sqlDate
	err := r.db.QueryRowContext(ctx, "SELECT id,ban_kind,expires_at FROM ip_auto_ban_events WHERE ip_address=? AND status='active' AND expires_at>? AND NOT EXISTS(SELECT 1 FROM ip_permanent_whitelist WHERE ip_address=?) ORDER BY banned_at DESC,id DESC LIMIT 1", ip, timestamp(time.Now()), ip).Scan(&id, &kind, &expiry)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &store.IPBan{ID: id, IP: ip, Kind: kind, ExpiresAt: expiry.Time}, nil
}
func (r *Repository) policyGeneration(ctx context.Context, q queryer) error {
	_, err := q.ExecContext(ctx, "UPDATE ip_security_projection SET generation=generation+1 WHERE singleton=1")
	return err
}
func (r *Repository) RecordInvalidAPI(ctx context.Context, ip, method, path, ua string, threshold, window int, audit store.AdminAudit) (int, error) {
	count := 0
	err := r.write(ctx, func(q queryer) error {
		if err := r.lockIP(ctx, q, ip); err != nil {
			return err
		}
		var whitelisted int
		err := q.QueryRowContext(ctx, "SELECT 1 FROM ip_permanent_whitelist WHERE ip_address=?"+r.lock(), ip).Scan(&whitelisted)
		if err == nil {
			count = 0
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		now := time.Now().UTC()
		cutoff := timestamp(now.Add(-time.Duration(window) * time.Second))
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM (SELECT id FROM ip_security_audit_log WHERE ip_address=? AND action='invalid_api' AND created_at>? ORDER BY created_at DESC,id DESC LIMIT ?) recent", ip, cutoff, threshold+1).Scan(&count); err != nil {
			return err
		}
		count++
		detail := map[string]any{"method": bounded(method, 16), "path": bounded(path, 2048), "user_agent": bounded(ua, 512), "window_count": count}
		if err := r.ipAudit(ctx, q, ip, "invalid_api", detail, audit); err != nil {
			return err
		}
		query := "INSERT INTO ip_security_summary(ip_address,attack_count,last_attack_at) VALUES (?,1,?)"
		if r.backend == "sqlite" {
			query += " ON CONFLICT(ip_address) DO UPDATE SET attack_count=ip_security_summary.attack_count+1,last_attack_at=MAX(COALESCE(last_attack_at,excluded.last_attack_at),excluded.last_attack_at)"
		} else {
			query += " ON DUPLICATE KEY UPDATE attack_count=attack_count+1,last_attack_at=GREATEST(COALESCE(last_attack_at,VALUES(last_attack_at)),VALUES(last_attack_at))"
		}
		if _, err := q.ExecContext(ctx, query, ip, timestamp(now)); err != nil {
			return err
		}
		if count <= threshold {
			return nil
		}
		if _, err := q.ExecContext(ctx, "UPDATE ip_auto_ban_events SET status='expired' WHERE ip_address=? AND status='active' AND expires_at<=?", ip, timestamp(now)); err != nil {
			return err
		}
		var active int
		err = q.QueryRowContext(ctx, "SELECT 1 FROM ip_auto_ban_events WHERE ip_address=? AND status='active' LIMIT 1"+r.lock(), ip).Scan(&active)
		if err == nil {
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		var previous int
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM ip_auto_ban_events WHERE ip_address=? AND ban_kind IN ('auto','permanent')", ip).Scan(&previous); err != nil {
			return err
		}
		kind := "auto"
		expiry := now.Add(24 * time.Hour)
		if previous > 0 {
			kind = "permanent"
			expiry = time.Date(9999, 12, 31, 23, 59, 59, 0, time.UTC)
		}
		var earliest sqlDate
		if err := q.QueryRowContext(ctx, "SELECT MIN(created_at) FROM (SELECT created_at FROM ip_security_audit_log WHERE ip_address=? AND action='invalid_api' AND created_at>? ORDER BY created_at DESC,id DESC LIMIT ?) recent", ip, cutoff, threshold+1).Scan(&earliest); err != nil {
			return err
		}
		if !earliest.Valid {
			earliest.Time = now
		}
		if _, err := q.ExecContext(ctx, "INSERT INTO ip_auto_ban_events(ip_address,trigger_count,window_started_at,banned_at,expires_at,last_method,last_path,user_agent,ban_kind,status) VALUES (?,?,?,?,?,?,?,?,?,'active')", ip, count, timestamp(earliest.Time), timestamp(now), timestamp(expiry), bounded(method, 16), bounded(path, 2048), bounded(ua, 512), kind); err != nil {
			return err
		}
		detail["ban_kind"] = kind
		detail["expires_at"] = isoTime(expiry, false)
		if err := r.ipAudit(ctx, q, ip, "automatic_ban", detail, audit); err != nil {
			return err
		}
		return r.policyGeneration(ctx, q)
	})
	return count, err
}
func (r *Repository) SetIPPolicy(ctx context.Context, ip, action, note string, audit store.AdminAudit) (*store.IPBan, error) {
	if (action == "reban" || action == "permanent_ban") && (note == "" || len([]rune(note)) > 255) {
		return nil, store.ErrPolicy
	}
	var ban *store.IPBan
	err := r.write(ctx, func(q queryer) error {
		ban = nil
		if err := r.lockIP(ctx, q, ip); err != nil {
			return err
		}
		now := time.Now().UTC()
		stamp := timestamp(now)
		ipAction := ""
		switch action {
		case "unban":
			if _, err := q.ExecContext(ctx, "UPDATE ip_auto_ban_events SET status='unbanned',released_at=?,released_by_session_hash=? WHERE ip_address=? AND status='active'", stamp, optional(audit.SessionHash), ip); err != nil {
				return err
			}
			ipAction = "unban"
		case "whitelist":
			query := "INSERT INTO ip_permanent_whitelist(ip_address,created_at,created_by_session_hash,note) VALUES (?,?,?,?)"
			if r.backend == "sqlite" {
				query += " ON CONFLICT(ip_address) DO UPDATE SET note=excluded.note"
			} else {
				query += " ON DUPLICATE KEY UPDATE note=VALUES(note)"
			}
			if _, err := q.ExecContext(ctx, query, ip, stamp, optional(audit.SessionHash), bounded(note, 255)); err != nil {
				return err
			}
			if _, err := q.ExecContext(ctx, "UPDATE ip_auto_ban_events SET status='whitelisted',released_at=?,released_by_session_hash=? WHERE ip_address=? AND status='active'", stamp, optional(audit.SessionHash), ip); err != nil {
				return err
			}
			ipAction = "whitelist_add"
		case "whitelist_remove":
			if _, err := q.ExecContext(ctx, "DELETE FROM ip_permanent_whitelist WHERE ip_address=?", ip); err != nil {
				return err
			}
			ipAction = "whitelist_remove"
		case "reban", "permanent_ban":
			var allowed int
			err := q.QueryRowContext(ctx, "SELECT 1 FROM ip_permanent_whitelist WHERE ip_address=?"+r.lock(), ip).Scan(&allowed)
			if err == nil {
				return store.ErrPolicy
			}
			if !errors.Is(err, sql.ErrNoRows) {
				return err
			}
			if _, err := q.ExecContext(ctx, "UPDATE ip_auto_ban_events SET status='expired' WHERE ip_address=? AND status='active' AND expires_at<=?", ip, stamp); err != nil {
				return err
			}
			var kind string
			err = q.QueryRowContext(ctx, "SELECT ban_kind FROM ip_auto_ban_events WHERE ip_address=? AND status='active' LIMIT 1"+r.lock(), ip).Scan(&kind)
			if err == nil && (action == "reban" || kind == "permanent") {
				return store.ErrPolicy
			}
			if err != nil && !errors.Is(err, sql.ErrNoRows) {
				return err
			}
			if action == "permanent_ban" {
				if _, err := q.ExecContext(ctx, "UPDATE ip_auto_ban_events SET status='replaced',released_at=?,released_by_session_hash=? WHERE ip_address=? AND status='active'", stamp, optional(audit.SessionHash), ip); err != nil {
					return err
				}
			}
			expiry := now.Add(24 * time.Hour)
			kind = "manual"
			ipAction = "manual_ban"
			path := "manual-reban"
			if action == "permanent_ban" {
				expiry = time.Date(9999, 12, 31, 23, 59, 59, 0, time.UTC)
				kind = "permanent"
				ipAction = "permanent_ban"
				path = "manual-permanent-ban"
			}
			result, err := q.ExecContext(ctx, "INSERT INTO ip_auto_ban_events(ip_address,trigger_count,window_started_at,banned_at,expires_at,last_method,last_path,ban_kind,reason,created_by_session_hash,status) VALUES (?,0,?,?,?,'ADMIN',?,?,?,?, 'active')", ip, stamp, stamp, timestamp(expiry), path, kind, bounded(note, 255), optional(audit.SessionHash))
			if err != nil {
				return err
			}
			id, err := result.LastInsertId()
			if err != nil {
				return err
			}
			ban = &store.IPBan{ID: id, IP: ip, Kind: kind, ExpiresAt: expiry}
		default:
			return store.ErrPolicy
		}
		if err := r.ipAudit(ctx, q, ip, ipAction, map[string]any{"note": bounded(note, 255)}, audit); err != nil {
			return err
		}
		audit.Action = "security_" + action
		audit.Result = "success"
		audit.TargetCount = 1
		audit.SourceSummary = ip
		audit.Detail = note
		if err := appendAudit(ctx, q, audit); err != nil {
			return err
		}
		return r.policyGeneration(ctx, q)
	})
	return ban, err
}
func (r *Repository) EdgeBans(ctx context.Context) ([]store.EdgeBan, int64, error) {
	var generation int64
	if err := r.db.QueryRowContext(ctx, "SELECT generation FROM ip_security_projection WHERE singleton=1").Scan(&generation); err != nil {
		return nil, 0, err
	}
	rows, err := r.db.QueryContext(ctx, "SELECT b.ip_address,b.expires_at,b.ban_kind FROM ip_auto_ban_events b WHERE b.status='active' AND b.expires_at>? AND NOT EXISTS(SELECT 1 FROM ip_permanent_whitelist w WHERE w.ip_address=b.ip_address)", timestamp(time.Now()))
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	result := []store.EdgeBan{}
	for rows.Next() {
		var v store.EdgeBan
		var expiry sqlDate
		var kind string
		if err := rows.Scan(&v.IP, &expiry, &kind); err != nil {
			return nil, 0, err
		}
		v.ExpiresAt = expiry.Time
		v.Permanent = kind == "permanent"
		result = append(result, v)
	}
	return result, generation, rows.Err()
}
func (r *Repository) AcknowledgeEdge(ctx context.Context, generation int64) error {
	_, err := r.db.ExecContext(ctx, "UPDATE ip_security_projection SET published_generation=? WHERE singleton=1 AND generation>=? AND published_generation<=?", generation, generation, generation)
	return err
}

func (r *Repository) EdgeProjection(ctx context.Context) (int64, int64, error) {
	var generation, published int64
	err := r.db.QueryRowContext(ctx, "SELECT generation,published_generation FROM ip_security_projection WHERE singleton=1").Scan(&generation, &published)
	return generation, published, err
}
