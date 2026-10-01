package business

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (r *Repository) SecuritySummary(ctx context.Context, f store.SecurityFilter) (store.SecuritySummary, error) {
	result := store.SecuritySummary{Events: []store.SecurityItem{}, Whitelist: []store.WhitelistItem{}}
	order := f.SortOrder
	if order == "" {
		order = "ip_" + f.IPOrder
	}
	var orderSQL string
	switch order {
	case "ip_asc":
		orderSQL = "LENGTH(INET6_ATON(ip_address)) ASC,INET6_ATON(ip_address) ASC,ip_address ASC"
	case "ip_desc":
		orderSQL = "LENGTH(INET6_ATON(ip_address)) DESC,INET6_ATON(ip_address) DESC,ip_address DESC"
	case "last_attack_asc", "last_attack_desc":
		direction := "ASC"
		if order == "last_attack_desc" {
			direction = "DESC"
		}
		orderSQL = "(last_attack_at IS NULL) ASC,last_attack_at " + direction + ",LENGTH(INET6_ATON(ip_address)) ASC,INET6_ATON(ip_address) ASC,ip_address ASC"
	default:
		return result, store.ErrPolicy
	}
	if f.Page < 1 || f.PageSize < 1 || f.PageSize > 200 || f.Window < 1 {
		return result, store.ErrPolicy
	}
	if f.MatchMode != "exact" && f.MatchMode != "fuzzy" {
		return result, store.ErrPolicy
	}
	if f.MatchMode == "fuzzy" {
		if f.Page > 20 || f.PageSize > 50 {
			return result, store.ErrPolicy
		}
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, 250*time.Millisecond)
		defer cancel()
	}
	now := time.Now().UTC()
	cte := `WITH ranked_bans AS (
 SELECT b.*,COUNT(*) OVER (PARTITION BY ip_address) AS ban_count,
 ROW_NUMBER() OVER (PARTITION BY ip_address ORDER BY (status='active' AND expires_at>?) DESC,banned_at DESC,id DESC) AS position
 FROM ip_auto_ban_events b
), known_ips AS (
 SELECT ip_address FROM ranked_bans UNION SELECT ip_address FROM ip_permanent_whitelist UNION SELECT ip_address FROM ip_security_summary
), objects AS (
 SELECT k.ip_address,COALESCE(b.ban_count,0) AS ban_count,COALESCE(a.attack_count,0) AS attack_count,
 a.last_attack_at,b.ban_kind,b.reason,w.note,
 CASE WHEN w.ip_address IS NOT NULL THEN 'whitelisted'
 WHEN b.status='active' AND b.expires_at>? AND b.ban_kind='permanent' THEN 'permanent'
 WHEN b.status='active' AND b.expires_at>? THEN 'active'
 WHEN a.last_attack_at>? THEN 'observed'
 WHEN COALESCE(b.ban_count,0)>0 THEN 'history' ELSE NULL END AS status
 FROM known_ips k LEFT JOIN ranked_bans b ON b.ip_address=k.ip_address AND b.position=1
 LEFT JOIN ip_permanent_whitelist w ON w.ip_address=k.ip_address
 LEFT JOIN ip_security_summary a ON a.ip_address=k.ip_address
) `
	base := []any{timestamp(now), timestamp(now), timestamp(now), timestamp(now.Add(-time.Duration(f.Window) * time.Second))}
	conditions := []string{"status IS NOT NULL"}
	filters := []any{}
	if f.IP != "" {
		query := "ip_address=?"
		value := f.IP
		if f.MatchMode == "fuzzy" {
			query = "ip_address LIKE ?"
			value = "%" + value + "%"
		}
		conditions = append(conditions, query)
		filters = append(filters, value)
	}
	if f.Status != "" {
		switch f.Status {
		case "active", "observed", "history", "permanent", "whitelisted":
		default:
			return result, store.ErrPolicy
		}
		conditions = append(conditions, "status=?")
		filters = append(filters, f.Status)
	}
	where := " WHERE " + strings.Join(conditions, " AND ")
	args := append(append([]any{}, base...), filters...)
	if err := r.db.QueryRowContext(ctx, cte+"SELECT COUNT(*) FROM objects"+where, args...).Scan(&result.Total); err != nil {
		return result, fuzzyError(f.MatchMode, err)
	}
	if err := r.db.QueryRowContext(ctx, cte+"SELECT COALESCE(SUM(status IN ('active','permanent')),0),COALESCE(SUM(status='whitelisted'),0) FROM objects", base...).Scan(&result.ActiveCount, &result.WhitelistCount); err != nil {
		return result, fuzzyError(f.MatchMode, err)
	}
	query := cte + "SELECT ip_address,ban_count,attack_count,last_attack_at,ban_kind,reason,note,status FROM objects" + where + " ORDER BY " + orderSQL + " LIMIT ? OFFSET ?"
	args = append(args, f.PageSize, (f.Page-1)*f.PageSize)
	rows, err := r.db.QueryContext(ctx, query, args...)
	if err != nil {
		return result, fuzzyError(f.MatchMode, err)
	}
	defer rows.Close()
	for rows.Next() {
		var item store.SecurityItem
		var last sqlDate
		var kind, reason, note sql.NullString
		if err := rows.Scan(&item.IP, &item.BanCount, &item.AttackCount, &last, &kind, &reason, &note, &item.Status); err != nil {
			return result, err
		}
		if last.Valid {
			value := isoTime(last.Time, true)
			item.LastAttack = &value
		}
		if item.Status == "whitelisted" {
			result.Whitelist = append(result.Whitelist, store.WhitelistItem{IP: item.IP, Note: note.String, AttackCount: item.AttackCount, LastAttack: item.LastAttack})
			continue
		}
		if kind.Valid {
			item.Kind = &kind.String
		}
		if reason.Valid {
			item.Reason = &reason.String
		}
		item.Active = item.Status == "active" || item.Status == "permanent"
		item.Permanent = item.Status == "permanent"
		result.Events = append(result.Events, item)
	}
	return result, fuzzyError(f.MatchMode, rows.Err())
}
func fuzzyError(mode string, err error) error {
	if mode == "fuzzy" && (errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled)) {
		return store.ErrQueryTimeout
	}
	return err
}
