package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// Rename manifests share the durable mutation ledger, not a new logical table.
// Their explicit state/format separate them from file-deletion manifests. A
// pending rename must be drained before handing the database back to Python.
func decodeGlobalRename(id, state string, raw []byte) (op store.GlobalRenameOperation, err error) {
	if len(raw) > 16*1024*1024 {
		return op, nodeConflict("oversized rename manifest")
	}
	err = json.Unmarshal(raw, &op)
	if err != nil {
		return
	}
	op.State = state
	if op.Format != "frontiercloud-global-rename" || op.Version != 1 || op.ID != id || !nodeIDPattern.MatchString(id) || !nodeIDPattern.MatchString(op.MasterID) || !store.ValidDirectoryRename(op.Old, op.New) || state != "rename_pending" && state != "rename_cleanup" && state != "rename_done" || len(op.Media) == 0 || len(op.Media) > 5000 {
		return op, nodeConflict("invalid rename manifest")
	}
	seen := map[string]bool{}
	for _, v := range op.Media {
		if seen[v.ID] || !nodeHashPattern.MatchString(v.ID) || !nodeHashPattern.MatchString(v.ObjectID) || !nodeIDPattern.MatchString(v.MemberID) || !strings.HasPrefix(v.Path, op.Old+"/") || !poolPath(store.LocalMedia{MediaObject: store.MediaObject{Path: v.Path, Kind: v.Kind}, Bytes: v.Bytes}) || v.Bytes < 0 || v.Bytes > store.MaxStorageAllocation || len(v.ETag) > 128 || strings.ContainsAny(v.ETag, "\r\n\x00") || v.MemberID != op.MasterID && (v.RelationshipID == nil || !nodeIDPattern.MatchString(*v.RelationshipID)) {
			return op, nodeConflict("invalid rename placement manifest")
		}
		seen[v.ID] = true
	}
	return
}

func (r *Repository) globalRename(ctx context.Context, q queryer, id string, lock bool) (*store.GlobalRenameOperation, error) {
	if !nodeIDPattern.MatchString(id) {
		return nil, store.ErrNodeState
	}
	query := "SELECT state,manifest FROM media_delete_operations WHERE operation_id=? AND state IN ('rename_pending','rename_cleanup','rename_done')"
	if lock {
		query += r.lock()
	}
	var state string
	var raw []byte
	err := q.QueryRowContext(ctx, query, id).Scan(&state, &raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	op, err := decodeGlobalRename(id, state, raw)
	return &op, err
}

func (r *Repository) GlobalRename(ctx context.Context, id string) (*store.GlobalRenameOperation, error) {
	return r.globalRename(ctx, r.db, id, false)
}

func (r *Repository) pendingGlobalRenames(ctx context.Context, q queryer, limit int, lock bool) ([]store.GlobalRenameOperation, error) {
	query := "SELECT operation_id,state,manifest FROM media_delete_operations WHERE state IN ('rename_pending','rename_cleanup') ORDER BY created_at,operation_id"
	args := []any{}
	if limit > 0 {
		query += " LIMIT ?"
		args = append(args, limit)
	}
	if lock {
		query += r.lock()
	}
	rows, err := q.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []store.GlobalRenameOperation{}
	for rows.Next() {
		var id, state string
		var raw []byte
		if err := rows.Scan(&id, &state, &raw); err != nil {
			return nil, err
		}
		op, err := decodeGlobalRename(id, state, raw)
		if err != nil {
			return nil, err
		}
		result = append(result, op)
	}
	return result, rows.Err()
}

func (r *Repository) PendingGlobalRenames(ctx context.Context, limit int) ([]store.GlobalRenameOperation, error) {
	n, err := r.ReadIdentity(ctx)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if n.Role != "Master" {
		return nil, nil
	}
	if limit < 1 || limit > 100 {
		limit = 10
	}
	return r.pendingGlobalRenames(ctx, r.db, limit, false)
}

func pathsOverlap(a, b string) bool {
	return a == b || strings.HasPrefix(a, b+"/") || strings.HasPrefix(b, a+"/")
}
func (r *Repository) checkGlobalRenameScopes(ctx context.Context, q queryer, scopes []store.DeleteItem) error {
	operations, err := r.pendingGlobalRenames(ctx, q, 0, true)
	if err != nil {
		return err
	}
	for _, op := range operations {
		for _, scope := range scopes {
			for _, name := range []string{op.Old, op.New} {
				if scope.Path == name || strings.HasPrefix(scope.Path, name+"/") || scope.Directory && strings.HasPrefix(name, scope.Path+"/") {
					return nodeConflict("path has pending directory rename")
				}
			}
		}
	}
	return nil
}

func (r *Repository) PrepareGlobalRename(ctx context.Context, old, target string, a store.AdminAudit) (op store.GlobalRenameOperation, err error) {
	if !store.ValidDirectoryRename(old, target) {
		return op, store.ErrNodeState
	}
	id, e := randomID()
	if e != nil {
		return op, e
	}
	err = r.write(ctx, func(q queryer) error {
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if n.Role != "Master" {
			return store.ErrNodeState
		}
		pending, err := r.pendingGlobalRenames(ctx, q, 0, true)
		if err != nil {
			return err
		}
		for _, p := range pending {
			if p.Old == old && p.New == target && p.MasterID == n.ID {
				op = p
				return nil
			}
			if pathsOverlap(old, p.Old) || pathsOverlap(old, p.New) || pathsOverlap(target, p.Old) || pathsOverlap(target, p.New) {
				return nodeConflict("directory has pending rename")
			}
		}
		for _, name := range []string{old, target} {
			where, args := r.pathScope("media_path", name, true)
			var count int
			if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM cluster_upload_sessions WHERE state='reserved' AND "+where, args...).Scan(&count); err != nil {
				return err
			}
			if count != 0 {
				return nodeConflict("directory has reserved uploads")
			}
		}
		where, args := r.pathScope("g.media_path", old, true)
		rows, err := q.QueryContext(ctx, "SELECT "+globalColumns+globalTables+" WHERE "+where+" ORDER BY g.media_id"+r.lock(), args...)
		if err != nil {
			return err
		}
		media := []store.GlobalMedia{}
		for rows.Next() {
			v, err := scanGlobal(rows)
			if err != nil {
				rows.Close()
				return err
			}
			media = append(media, v)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		if len(media) == 0 {
			return os.ErrNotExist
		}
		if len(media) > 5000 {
			return nodeConflict("directory rename selection too large")
		}
		for _, v := range media {
			if v.State != "active" {
				return nodeConflict("directory has pending mutation")
			}
			if v.MemberID == n.ID {
				continue
			}
			if v.RelationshipID == nil || v.Health != "online" {
				return nodeConflict("storage owner is offline")
			}
			rel, err := scanRelation(q.QueryRowContext(ctx, "SELECT "+relationColumns+" FROM node_relationships WHERE relationship_id=?"+r.lock(), *v.RelationshipID))
			if err != nil {
				return err
			}
			if rel.State != "active" || rel.Direction != "downstream" || rel.PeerID != v.MemberID || rel.Protocol != 2 || rel.Status != "online" || time.Now().Unix()-rel.LastHeartbeat >= 120 {
				return nodeConflict("storage owner is not reachable")
			}
		}
		where, args = r.pathScope("media_path", target, true)
		var count int
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM global_media_objects WHERE "+where, args...).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return os.ErrExist
		}
		if err := r.checkRename(ctx, q, target); err != nil {
			return err
		}
		op = store.GlobalRenameOperation{Format: "frontiercloud-global-rename", Version: 1, ID: id[:32], MasterID: n.ID, Old: old, New: target, State: "rename_pending", Media: media, Audit: a}
		raw, err := json.Marshal(op)
		if err != nil {
			return err
		}
		if _, err := decodeGlobalRename(op.ID, op.State, raw); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "INSERT INTO media_delete_operations(operation_id,state,manifest,created_at) VALUES (?,'rename_pending',?,?)", op.ID, string(raw), timestamp(time.Now())); err != nil {
			return err
		}
		for _, v := range media {
			if _, err := q.ExecContext(ctx, "UPDATE global_media_objects SET state='renaming',updated_at=? WHERE media_id=?", time.Now().Unix(), v.ID); err != nil {
				return err
			}
		}
		a.Action, a.Result, a.TargetCount = "global-directory-rename", "pending", len(media)
		a.SourceSummary = renameSource(old, target, op.ID, "")
		a.Detail = a.SourceSummary
		return appendAudit(ctx, q, a)
	})
	return
}

// Call only after every owner supplied a verified destination placement proof.
// Catalog paths/state, local metadata, completed sessions and success audit are
// atomic. Neither local nor remote storage capacity changes on rename.
func (r *Repository) CompleteGlobalRename(ctx context.Context, id string) error {
	return r.write(ctx, func(q queryer) error {
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		op, err := r.globalRename(ctx, q, id, true)
		if err != nil {
			return err
		}
		if op == nil || n.Role != "Master" || n.ID != op.MasterID {
			return store.ErrNodeState
		}
		if op.State == "rename_done" || op.State == "rename_cleanup" {
			return nil
		}
		for _, v := range op.Media {
			actual, err := scanGlobal(q.QueryRowContext(ctx, "SELECT "+globalColumns+globalTables+" WHERE g.media_id=?"+r.lock(), v.ID))
			if err != nil {
				return err
			}
			if actual.State != "renaming" || actual.Path != v.Path || actual.MemberID != v.MemberID || actual.ObjectID != v.ObjectID || actual.Bytes != v.Bytes || actual.ETag != v.ETag {
				return nodeConflict("rename placement changed")
			}
		}
		if err := r.completeRename(ctx, q, op.Old, op.New, id, "", op.Audit); err != nil {
			return err
		}
		for _, v := range op.Media {
			name := op.New + strings.TrimPrefix(v.Path, op.Old)
			if _, err := q.ExecContext(ctx, "UPDATE global_media_objects SET media_path=?,path_locator=?,state='active',updated_at=? WHERE media_id=?", name, locator(name), time.Now().Unix(), v.ID); err != nil {
				return err
			}
			if _, err := q.ExecContext(ctx, "UPDATE cluster_upload_sessions SET media_path=?,updated_at=? WHERE media_id=? AND state='complete'", name, time.Now().Unix(), v.ID); err != nil {
				return err
			}
		}
		_, err = q.ExecContext(ctx, "UPDATE media_delete_operations SET state='rename_cleanup' WHERE operation_id=?", id)
		return err
	})
}

func (r *Repository) DeferGlobalRename(ctx context.Context, id string) error {
	if !nodeIDPattern.MatchString(id) {
		return store.ErrNodeState
	}
	return r.write(ctx, func(q queryer) error {
		_, err := q.ExecContext(ctx, "UPDATE media_delete_operations SET created_at=? WHERE operation_id=? AND state IN ('rename_pending','rename_cleanup')", timestamp(time.Now()), id)
		return err
	})
}

// The service has durably removed this operation's exact local ownership marker
// (or the operation never had a local placement). This final state releases both
// namespace fences; a crash before it remains discoverable by background retry.
func (r *Repository) CompleteGlobalRenameCleanup(ctx context.Context, id string) error {
	return r.write(ctx, func(q queryer) error {
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		op, err := r.globalRename(ctx, q, id, true)
		if err != nil {
			return err
		}
		if op == nil || n.Role != "Master" || n.ID != op.MasterID || op.State == "rename_pending" {
			return store.ErrNodeState
		}
		_, err = q.ExecContext(ctx, "UPDATE media_delete_operations SET state='rename_done' WHERE operation_id=? AND state='rename_cleanup'", id)
		return err
	})
}
