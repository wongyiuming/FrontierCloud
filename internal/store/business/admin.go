package business

import (
	"context"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"time"
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
	_, err := r.db.ExecContext(ctx, "INSERT INTO admin_audit_log (session_id_hash,action,target_count,source_summary,result,detail,client_ip,user_agent,request_id,trace_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", optional(a.SessionHash), bounded(a.Action, 64), a.TargetCount, bounded(a.SourceSummary, 10000), bounded(a.Result, 32), bounded(a.Detail, 10000), bounded(a.ClientIP, 45), bounded(a.UserAgent, 512), optional(bounded(a.RequestID, 128)), optional(a.TraceID), timestamp(time.Now()))
	return err
}
