package business

import (
	"context"
	"database/sql"
	"sort"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (r *Repository) RecordObservations(ctx context.Context, client string, addresses []string, outcome string) error {
	addresses = append([]string{}, addresses...)
	if len(addresses) == 0 {
		addresses = append(addresses, "")
	}
	sort.Strings(addresses)
	return r.write(ctx, func(q queryer) error {
		now := timestamp(time.Now())
		query := "INSERT INTO webrtc_observation_summary(client_ip,webrtc_ip_key,webrtc_ip,observation_count,matching_count,first_seen,last_seen,last_outcome) VALUES (?,?,?,1,?,?,?,?)"
		if r.backend == "sqlite" {
			query += " ON CONFLICT(client_ip,webrtc_ip_key) DO UPDATE SET observation_count=webrtc_observation_summary.observation_count+1,matching_count=webrtc_observation_summary.matching_count+excluded.matching_count,first_seen=MIN(first_seen,excluded.first_seen),last_outcome=CASE WHEN excluded.last_seen>=last_seen THEN excluded.last_outcome ELSE last_outcome END,last_seen=MAX(last_seen,excluded.last_seen)"
		} else {
			query += " ON DUPLICATE KEY UPDATE observation_count=observation_count+1,matching_count=matching_count+VALUES(matching_count),first_seen=LEAST(first_seen,VALUES(first_seen)),last_outcome=IF(VALUES(last_seen)>=last_seen,VALUES(last_outcome),last_outcome),last_seen=GREATEST(last_seen,VALUES(last_seen))"
		}
		for _, address := range addresses {
			matching := address != "" && address == client
			if _, err := q.ExecContext(ctx, "INSERT INTO webrtc_observation_events(client_ip,webrtc_ip,outcome,matches_verified,observed_at) VALUES (?,?,?,?,?)", client, optional(address), outcome, matching, now); err != nil {
				return err
			}
			if _, err := q.ExecContext(ctx, query, client, address, optional(address), matching, now, now, outcome); err != nil {
				return err
			}
		}
		return nil
	})
}
func observationRows(rows *sql.Rows) ([]store.Observation, error) {
	result := []store.Observation{}
	for rows.Next() {
		var value store.Observation
		var observed sql.NullString
		var first, last sqlDate
		if err := rows.Scan(&value.ClientIP, &observed, &value.Count, &value.MatchingCount, &first, &last, &value.Outcome); err != nil {
			return nil, err
		}
		if observed.Valid {
			value.WebRTCIP = &observed.String
		}
		value.FirstSeen = isoTime(first.Time, true)
		value.LastSeen = isoTime(last.Time, true)
		result = append(result, value)
	}
	return result, rows.Err()
}
func (r *Repository) Observations(ctx context.Context, f store.ObservationFilter) (store.ObservationSummary, error) {
	result := store.ObservationSummary{Items: []store.Observation{}, Groups: []store.ObservationGroup{}}
	if f.Page < 1 || f.PageSize < 1 || f.PageSize > 200 || f.View != "pairs" && f.View != "public" && f.View != "webrtc" || f.MatchMode != "exact" && f.MatchMode != "fuzzy" {
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
	clauses := []string{}
	args := []any{}
	for _, filter := range []struct{ col, val string }{{"client_ip", f.PublicIP}, {"webrtc_ip", f.WebRTCIP}} {
		if filter.val == "" {
			continue
		}
		query := filter.col + "=?"
		val := filter.val
		if f.MatchMode == "fuzzy" {
			query = filter.col + " LIKE ?"
			val = "%" + val + "%"
		}
		clauses = append(clauses, query)
		args = append(args, val)
	}
	if f.View == "webrtc" {
		clauses = append(clauses, "webrtc_ip IS NOT NULL")
	}
	where := ""
	if len(clauses) > 0 {
		where = " WHERE " + strings.Join(clauses, " AND ")
	}
	columns := "client_ip,webrtc_ip,observation_count,matching_count,first_seen,last_seen,last_outcome"
	if f.View == "pairs" {
		if err := r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM webrtc_observation_summary"+where, args...).Scan(&result.Total); err != nil {
			return result, fuzzyError(f.MatchMode, err)
		}
		pageArgs := append(append([]any{}, args...), f.PageSize, (f.Page-1)*f.PageSize)
		rows, err := r.db.QueryContext(ctx, "SELECT "+columns+" FROM webrtc_observation_summary"+where+" ORDER BY last_seen DESC,client_ip ASC,webrtc_ip ASC LIMIT ? OFFSET ?", pageArgs...)
		if err != nil {
			return result, fuzzyError(f.MatchMode, err)
		}
		defer rows.Close()
		result.Items, err = observationRows(rows)
		return result, fuzzyError(f.MatchMode, err)
	}
	groupColumn := "client_ip"
	if f.View == "webrtc" {
		groupColumn = "webrtc_ip"
	}
	if err := r.db.QueryRowContext(ctx, "SELECT COUNT(DISTINCT "+groupColumn+") FROM webrtc_observation_summary"+where, args...).Scan(&result.Total); err != nil {
		return result, fuzzyError(f.MatchMode, err)
	}
	pageArgs := append(append([]any{}, args...), f.PageSize, (f.Page-1)*f.PageSize)
	rows, err := r.db.QueryContext(ctx, "SELECT "+groupColumn+",COUNT(*),SUM(observation_count),MIN(first_seen),MAX(last_seen) FROM webrtc_observation_summary"+where+" GROUP BY "+groupColumn+" ORDER BY MAX(last_seen) DESC,"+groupColumn+" ASC LIMIT ? OFFSET ?", pageArgs...)
	if err != nil {
		return result, fuzzyError(f.MatchMode, err)
	}
	indices := map[string]int{}
	for rows.Next() {
		var group store.ObservationGroup
		var first, last sqlDate
		if err := rows.Scan(&group.Key, &group.RelationCount, &group.Count, &first, &last); err != nil {
			rows.Close()
			return result, err
		}
		group.FirstSeen = isoTime(first.Time, true)
		group.LastSeen = isoTime(last.Time, true)
		group.Relations = []store.Observation{}
		indices[group.Key] = len(result.Groups)
		result.Groups = append(result.Groups, group)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return result, fuzzyError(f.MatchMode, err)
	}
	if len(result.Groups) == 0 {
		return result, nil
	}
	placeholders := []string{}
	detailArgs := append([]any{}, args...)
	for _, group := range result.Groups {
		placeholders = append(placeholders, "?")
		detailArgs = append(detailArgs, group.Key)
	}
	conditions := append(append([]string{}, clauses...), groupColumn+" IN ("+strings.Join(placeholders, ",")+")")
	rows, err = r.db.QueryContext(ctx, "SELECT "+columns+" FROM webrtc_observation_summary WHERE "+strings.Join(conditions, " AND ")+" ORDER BY "+groupColumn+" ASC,last_seen DESC,client_ip ASC,webrtc_ip ASC", detailArgs...)
	if err != nil {
		return result, fuzzyError(f.MatchMode, err)
	}
	defer rows.Close()
	items, err := observationRows(rows)
	if err != nil {
		return result, fuzzyError(f.MatchMode, err)
	}
	for _, item := range items {
		key := item.ClientIP
		if f.View == "webrtc" {
			key = *item.WebRTCIP
		}
		if index, ok := indices[key]; ok {
			result.Groups[index].Relations = append(result.Groups[index].Relations, item)
		}
	}
	return result, nil
}
