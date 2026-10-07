package business

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// Offline-only history compaction, never recovery of a pending operation. The
// caller retains lifecycle + media leases and proves no physical intent remains.
// Success audit and this manifest's digest survive removal of the completed row.
func (r *Repository) DrainCompletedRenames(ctx context.Context, confirmation string, a store.NodeAudit) (drained int, err error) {
	if !nodeIDPattern.MatchString(confirmation) {
		return 0, store.ErrNodeState
	}
	err = r.write(ctx, func(q queryer) error {
		drained = 0
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if n.ID != confirmation {
			return store.ErrNodeState
		}
		var pending int64
		if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM media_delete_operations WHERE state<>'rename_done'").Scan(&pending); err != nil {
			return err
		}
		if pending != 0 {
			return store.ErrBackupBusy
		}
		rows, err := q.QueryContext(ctx, "SELECT operation_id,LENGTH(manifest),SUBSTR(manifest,1,16777217) FROM media_delete_operations WHERE state='rename_done' ORDER BY operation_id LIMIT 1001"+r.lock())
		if err != nil {
			return err
		}
		type entry struct {
			id, master, old, target, digest string
			count                           int
		}
		var entries []entry
		var total int64
		for rows.Next() {
			var id string
			var length int64
			var raw []byte
			if err := rows.Scan(&id, &length, &raw); err != nil {
				rows.Close()
				return err
			}
			total += int64(len(raw))
			if length <= 0 || length > 16*1024*1024 || total > 64*1024*1024 || len(entries) >= 1000 {
				rows.Close()
				return store.ErrBackupState
			}
			op, err := decodeGlobalRename(id, "rename_done", raw)
			if err != nil {
				rows.Close()
				return err
			}
			// MySQL JSON normalizes whitespace/key ordering. Require exact typed
			// JSON semantics, not its textual layout; reject duplicate/unknown
			// keys, wrong-case fields, trailing values and lossy numeric types.
			value, err := protocol.ParseStrictJSON(raw, 16*1024*1024)
			if err != nil {
				rows.Close()
				return store.ErrBackupState
			}
			actual, err := protocol.Canonical(value)
			if err != nil {
				rows.Close()
				return store.ErrBackupState
			}
			encoded, err := json.Marshal(op)
			if err != nil {
				rows.Close()
				return err
			}
			typed, err := protocol.ParseStrictJSON(encoded, 16*1024*1024)
			if err != nil {
				rows.Close()
				return err
			}
			expected, err := protocol.Canonical(typed)
			if err != nil || !bytes.Equal(actual, expected) {
				rows.Close()
				return store.ErrBackupState
			}
			for _, v := range op.Media {
				if v.State != "active" {
					rows.Close()
					return store.ErrBackupState
				}
			}
			hash := sha256.Sum256(raw)
			entries = append(entries, entry{id, op.MasterID, op.Old, op.New, hex.EncodeToString(hash[:]), len(op.Media)})
		}
		readErr := rows.Err()
		rows.Close()
		if readErr != nil {
			return readErr
		}
		for _, v := range entries {
			var count int64
			if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM admin_audit_log WHERE action='directory_rename' AND result='success' AND source_summary=?", renameSource(v.old, v.target, v.id, "")).Scan(&count); err != nil {
				return err
			}
			if count != 1 {
				return nodeConflict("completed rename has no unique exact success audit")
			}
			if err := r.nodeAudit(ctx, q, "rename-history-drained", "", map[string]any{"operation_id": v.id, "master_id": v.master, "old_path": v.old, "new_path": v.target, "objects": v.count, "manifest_sha256": v.digest}, a); err != nil {
				return err
			}
			result, err := q.ExecContext(ctx, "DELETE FROM media_delete_operations WHERE operation_id=? AND state='rename_done'", v.id)
			if err != nil {
				return err
			}
			count, err = result.RowsAffected()
			if err != nil {
				return err
			}
			if count != 1 {
				return store.ErrNodeState
			}
			drained++
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return drained, nil
}
