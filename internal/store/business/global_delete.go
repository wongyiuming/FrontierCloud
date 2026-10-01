package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"path"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (r *Repository) PrepareGlobalDelete(ctx context.Context, scopes []store.DeleteItem, a store.AdminAudit) (result []store.GlobalMedia, err error) {
	if len(scopes) == 0 || len(scopes) > 5000 {
		return nil, nodeConflict("invalid global deletion selection")
	}
	clauses, args := []string{}, []any{}
	for _, item := range scopes {
		parts := strings.Split(item.Path, "/")
		valid := path.Clean(item.Path) == item.Path && utf8.RuneCountInString(item.Path) <= 1024 && utf8.ValidString(item.Path) && !strings.ContainsAny(item.Path, "\\\x00:") && (parts[0] == "music" || parts[0] == "vido")
		for _, p := range parts {
			valid = valid && p != "" && !strings.HasPrefix(p, ".")
		}
		if !valid || item.Directory && len(parts) > 3 || !item.Directory && !poolPath(store.LocalMedia{MediaObject: store.MediaObject{Path: item.Path, Kind: map[string]string{"music": "audio", "vido": "video"}[parts[0]]}}) {
			return nil, nodeConflict("invalid global deletion path")
		}
		where, values := r.pathScope("g.media_path", item.Path, item.Directory)
		clauses = append(clauses, where)
		args = append(args, values...)
	}
	err = r.write(ctx, func(q queryer) error {
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if n.Role != "Master" {
			return nodeConflict("only Master deletes global media")
		}
		if err := r.checkGlobalRenameScopes(ctx, q, scopes); err != nil {
			return err
		}
		rows, err := q.QueryContext(ctx, "SELECT "+globalColumns+globalTables+" WHERE g.state IN ('active','pending_delete') AND ("+strings.Join(clauses, " OR ")+") ORDER BY g.media_id"+r.lock(), args...)
		if err != nil {
			return err
		}
		result = []store.GlobalMedia{}
		for rows.Next() {
			v, e := scanGlobal(rows)
			if e != nil {
				rows.Close()
				return e
			}
			result = append(result, v)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		if len(result) == 0 {
			return nodeConflict("global deletion selection not found")
		}
		changed := []string{}
		for i := range result {
			v := &result[i]
			if v.State == "active" {
				if _, err := q.ExecContext(ctx, "UPDATE global_media_objects SET state='pending_delete',updated_at=? WHERE media_id=?", time.Now().Unix(), v.ID); err != nil {
					return err
				}
				v.State = "pending_delete"
				changed = append(changed, v.Path)
			}
		}
		if len(changed) == 0 {
			return nil
		}
		a.Action, a.Result = "global-media-delete", "pending"
		return appendMutationAudit(ctx, q, a, changed)
	})
	return
}
func (r *Repository) PendingGlobalDeletes(ctx context.Context, limit int) ([]store.GlobalMedia, error) {
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
	if limit < 1 || limit > 500 {
		limit = 50
	}
	rows, err := r.db.QueryContext(ctx, "SELECT "+globalColumns+globalTables+" WHERE g.state='pending_delete' ORDER BY g.updated_at,g.media_id LIMIT ?", limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []store.GlobalMedia{}
	for rows.Next() {
		v, e := scanGlobal(rows)
		if e != nil {
			return nil, e
		}
		result = append(result, v)
	}
	return result, rows.Err()
}

func (r *Repository) GlobalPlacement(ctx context.Context, id string) (*store.GlobalMedia, error) {
	if !nodeHashPattern.MatchString(id) {
		return nil, store.ErrNodeState
	}
	n, err := r.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if n.Role != "Master" {
		return nil, store.ErrNodeState
	}
	v, err := scanGlobal(r.db.QueryRowContext(ctx, "SELECT "+globalColumns+globalTables+" WHERE g.media_id=?", id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &v, nil
}

func (r *Repository) DeferGlobalDelete(ctx context.Context, id string) error {
	if !nodeHashPattern.MatchString(id) {
		return store.ErrNodeState
	}
	return r.write(ctx, func(q queryer) error {
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if n.Role != "Master" {
			return store.ErrNodeState
		}
		_, err = q.ExecContext(ctx, "UPDATE global_media_objects SET updated_at=? WHERE media_id=? AND state='pending_delete'", time.Now().Unix(), id)
		return err
	})
}

// Called only after the owning storage node proved physical deletion. Local
// placement must use CommitMasterDelete, atomically with its quarantine journal.
func (r *Repository) CompleteGlobalDelete(ctx context.Context, id string, a store.AdminAudit) error {
	if !nodeHashPattern.MatchString(id) {
		return nodeConflict("invalid global deletion identity")
	}
	return r.write(ctx, func(q queryer) error {
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if n.Role != "Master" {
			return store.ErrNodeState
		}
		var owner string
		err = q.QueryRowContext(ctx, "SELECT storage_member_id FROM global_media_objects WHERE media_id=?"+r.lock(), id).Scan(&owner)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if owner == n.ID {
			return nodeConflict("local deletion requires a quarantine journal")
		}
		return r.completeGlobalDelete(ctx, q, id, a)
	})
}
func (r *Repository) completeGlobalDelete(ctx context.Context, q queryer, id string, a store.AdminAudit) error {
	v, err := scanGlobal(q.QueryRowContext(ctx, "SELECT "+globalColumns+globalTables+" WHERE g.media_id=?"+r.lock(), id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if v.State != "pending_delete" {
		return nodeConflict("global deletion was not prepared")
	}
	var used int64
	if err = q.QueryRowContext(ctx, "SELECT used_bytes FROM cluster_storage_members WHERE member_id=?"+r.lock(), v.MemberID).Scan(&used); err != nil {
		return err
	}
	if used < v.Bytes {
		return nodeConflict("global deletion accounting mismatch")
	}
	if err := r.deleteMetadata(ctx, q, store.DeleteItem{Path: v.Path}); err != nil {
		return err
	}
	for _, table := range []string{"media_playback_events", "media_playback_stats", "media_lyric_links"} {
		if _, err := q.ExecContext(ctx, "DELETE FROM "+table+" WHERE media_id=?", id); err != nil {
			return err
		}
	}
	if _, err := q.ExecContext(ctx, "DELETE FROM cluster_upload_sessions WHERE media_id=? AND state='complete'", id); err != nil {
		return err
	}
	if _, err := q.ExecContext(ctx, "DELETE FROM global_media_objects WHERE media_id=?", id); err != nil {
		return err
	}
	if _, err := q.ExecContext(ctx, "UPDATE cluster_storage_members SET used_bytes=used_bytes-?,updated_at=? WHERE member_id=?", v.Bytes, time.Now().Unix(), v.MemberID); err != nil {
		return err
	}
	a.Action, a.Result, a.TargetCount = "global-media-delete-completed", "success", 1
	detail, _ := json.Marshal(map[string]any{"media_id": id, "member_id": v.MemberID, "size_bytes": v.Bytes})
	a.Detail = string(detail)
	return appendMutationAudit(ctx, q, a, []string{v.Path})
}
func (r *Repository) PrepareMasterDelete(ctx context.Context, op store.DeleteOperation, a store.AdminAudit) error {
	item, err := ownedDeleteItem(op)
	if err != nil {
		return err
	}
	if !nodeHashPattern.MatchString(item.GlobalID) {
		return store.ErrNodeState
	}
	manifest, err := json.Marshal(op.Items)
	if err != nil {
		return err
	}
	return r.write(ctx, func(q queryer) error {
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if n.Role != "Master" {
			return store.ErrNodeState
		}
		var owner, object, name, state string
		var size int64
		if err := q.QueryRowContext(ctx, "SELECT storage_member_id,object_id,media_path,size_bytes,state FROM global_media_objects WHERE media_id=?"+r.lock(), item.GlobalID).Scan(&owner, &object, &name, &size, &state); err != nil {
			return err
		}
		if owner != n.ID || object != item.OwnedID || name != item.Path || size != item.Bytes || state != "pending_delete" {
			return nodeConflict("Master deletion manifest mismatch")
		}
		var objectPath string
		if err := q.QueryRowContext(ctx, "SELECT media_path FROM media_objects WHERE media_id=?"+r.lock(), item.OwnedID).Scan(&objectPath); err != nil {
			return err
		}
		if objectPath != name {
			return store.ErrNodeState
		}
		_, err = q.ExecContext(ctx, "INSERT INTO media_delete_operations(operation_id,state,manifest,created_at) VALUES (?,'pending',?,?)", op.ID, string(manifest), timestamp(time.Now()))
		return err
	})
}
func (r *Repository) CommitMasterDelete(ctx context.Context, id string, a store.AdminAudit) error {
	return r.write(ctx, func(q queryer) error {
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if n.Role != "Master" {
			return store.ErrNodeState
		}
		op, err := r.deleteOperation(ctx, q, id, true)
		if err != nil {
			return err
		}
		if op == nil {
			return store.ErrNodeState
		}
		item, err := ownedDeleteItem(*op)
		if err != nil {
			return err
		}
		if !nodeHashPattern.MatchString(item.GlobalID) {
			return store.ErrNodeState
		}
		if op.State == "committed" {
			return nil
		}
		var owner string
		if err := q.QueryRowContext(ctx, "SELECT storage_member_id FROM global_media_objects WHERE media_id=?"+r.lock(), item.GlobalID).Scan(&owner); err != nil {
			return err
		}
		if owner != n.ID {
			return store.ErrNodeState
		}
		if err := r.completeGlobalDelete(ctx, q, item.GlobalID, a); err != nil {
			return err
		}
		_, err = q.ExecContext(ctx, "UPDATE media_delete_operations SET state='committed' WHERE operation_id=?", id)
		return err
	})
}
