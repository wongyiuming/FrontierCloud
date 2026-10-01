package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// A private filesystem publication journal survives a terminated request. The
// operation ID in source_summary makes replay idempotent without a new schema.
func (r *Repository) CompleteUpload(ctx context.Context, object store.MediaObject, operation string, audit store.AdminAudit) error {
	return r.write(ctx, func(q queryer) error {
		id, err := r.ensureWithLock(ctx, q, object, r.lock())
		if err != nil {
			return err
		}
		// Serialize replay on the persistent object. Use a locking CURRENT read
		// for audit deduplication: MySQL's earlier repeatable-read snapshot can
		// predate the winning commit even after its object lock is acquired.
		if _, err := q.ExecContext(ctx, "UPDATE media_objects SET updated_at=? WHERE media_id=?", timestamp(time.Now()), id); err != nil {
			return err
		}
		source, _ := json.Marshal(map[string]any{"operation_id": operation, "paths": []string{object.Path}})
		var count int
		err = q.QueryRowContext(ctx, "SELECT 1 FROM admin_audit_log WHERE action=? AND source_summary=? AND result='success' LIMIT 1"+r.lock(), audit.Action, string(source)).Scan(&count)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if err == nil {
			return nil
		}
		audit.SourceSummary = string(source)
		audit.TargetCount = 1
		audit.Result = "success"
		detail, _ := json.Marshal(map[string]any{"operation_id": operation, "media_id": id, "path": object.Path})
		audit.Detail = string(detail)
		return appendAudit(ctx, q, audit)
	})
}
