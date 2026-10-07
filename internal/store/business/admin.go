package business

import (
	"context"
	"encoding/json"
	"time"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func bounded(value string, size int) string {
	runes := []rune(value)
	if len(runes) > size {
		runes = runes[:size]
	}
	return string(runes)
}
func optional(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func (r *Repository) AppendAudit(ctx context.Context, a store.AdminAudit) error {
	return appendAudit(ctx, r.db, a)
}

func appendAudit(ctx context.Context, q queryer, a store.AdminAudit) error {
	_, err := q.ExecContext(ctx, "INSERT INTO admin_audit_log (session_id_hash,action,target_count,source_summary,result,detail,client_ip,user_agent,request_id,trace_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", optional(a.SessionHash), bounded(a.Action, 64), a.TargetCount, bounded(a.SourceSummary, 10000), bounded(a.Result, 32), bounded(a.Detail, 10000), bounded(a.ClientIP, 45), bounded(a.UserAgent, 512), optional(bounded(a.RequestID, 128)), optional(a.TraceID), timestamp(time.Now()))
	return err
}

// Retain every target without silently truncating a large batch's evidence.
// All rows participate in the same business transaction as the mutation.
func appendMutationAudit(ctx context.Context, q queryer, a store.AdminAudit, paths []string) error {
	detail := map[string]any{}
	if a.Detail != "" {
		if err := json.Unmarshal([]byte(a.Detail), &detail); err != nil {
			return err
		}
	}
	detail["affected_total"] = len(paths)
	encoded, err := json.Marshal(detail)
	if err != nil {
		return err
	}
	a.Detail = string(encoded)
	batch := []string{}
	flush := func() error {
		source, err := json.Marshal(batch)
		if err != nil {
			return err
		}
		a.SourceSummary = string(source)
		a.TargetCount = len(batch)
		return appendAudit(ctx, q, a)
	}
	for _, path := range paths {
		candidate := append(append([]string{}, batch...), path)
		source, err := json.Marshal(candidate)
		if err != nil {
			return err
		}
		if len(batch) > 0 && utf8.RuneCount(source) > 8000 {
			if err := flush(); err != nil {
				return err
			}
			batch = []string{}
		}
		batch = append(batch, path)
	}
	return flush()
}
