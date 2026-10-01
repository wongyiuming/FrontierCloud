package business

import (
	"context"
	"encoding/json"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func ownedDeleteItem(operation store.DeleteOperation) (store.DeleteItem, error) {
	if !nodeIDPattern.MatchString(operation.ID) || operation.State != "pending" && operation.State != "committed" || len(operation.Items) != 1 {
		return store.DeleteItem{}, nodeConflict("invalid owned delete operation")
	}
	item := operation.Items[0]
	kind := "audio"
	if len(item.Path) >= 5 && item.Path[:5] == "vido/" {
		kind = "video"
	}
	if item.Directory || item.Slot != "0" || !nodeHashPattern.MatchString(item.OwnedID) || item.Bytes < 0 || !poolPath(store.LocalMedia{MediaObject: store.MediaObject{ID: item.OwnedID, Path: item.Path, Kind: kind}, Bytes: item.Bytes}) {
		return store.DeleteItem{}, nodeConflict("invalid owned delete manifest")
	}
	return item, nil
}
func (r *Repository) PrepareOwnedDelete(ctx context.Context, relationship string, operation store.DeleteOperation, a store.NodeAudit) error {
	item, err := ownedDeleteItem(operation)
	if err != nil {
		return err
	}
	if !nodeIDPattern.MatchString(relationship) {
		return nodeConflict("invalid owned delete relationship")
	}
	manifest, err := json.Marshal(operation.Items)
	if err != nil {
		return err
	}
	return r.write(ctx, func(q queryer) error {
		row, err := r.ownedStorageNode(ctx, q, relationship)
		if err != nil {
			return err
		}
		var path string
		if err = q.QueryRowContext(ctx, "SELECT media_path FROM media_objects WHERE media_id=?"+r.lock(), item.OwnedID).Scan(&path); err != nil {
			return err
		}
		if path != item.Path {
			return nodeConflict("owned delete identity mismatch")
		}
		var used int64
		if err = q.QueryRowContext(ctx, "SELECT used_bytes FROM cluster_storage_members WHERE member_id=?"+r.lock(), row.ID).Scan(&used); err != nil {
			return err
		}
		if used < item.Bytes {
			return nodeConflict("owned delete accounting mismatch")
		}
		if _, err = q.ExecContext(ctx, "INSERT INTO media_delete_operations(operation_id,state,manifest,created_at) VALUES (?,'pending',?,?)", operation.ID, string(manifest), timestamp(time.Now())); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "storage-delete-prepared", relationship, map[string]any{"operation": operation.ID, "object_id": item.OwnedID, "path": item.Path, "bytes": item.Bytes}, a)
	})
}
func (r *Repository) CommitOwnedDelete(ctx context.Context, id string, free int64, a store.NodeAudit) error {
	return r.write(ctx, func(q queryer) error {
		row, err := r.ownedStorageNode(ctx, q, "")
		if err != nil {
			return err
		}
		operation, err := r.deleteOperation(ctx, q, id, true)
		if err != nil {
			return err
		}
		if operation == nil {
			return nodeConflict("owned delete operation missing")
		}
		item, err := ownedDeleteItem(*operation)
		if err != nil {
			return err
		}
		if operation.State == "committed" {
			return nil
		}
		var path string
		if err = q.QueryRowContext(ctx, "SELECT media_path FROM media_objects WHERE media_id=?"+r.lock(), item.OwnedID).Scan(&path); err != nil {
			return err
		}
		if path != item.Path {
			return nodeConflict("owned delete identity mismatch")
		}
		var used int64
		if err = q.QueryRowContext(ctx, "SELECT used_bytes FROM cluster_storage_members WHERE member_id=?"+r.lock(), row.ID).Scan(&used); err != nil {
			return err
		}
		if used < item.Bytes {
			return nodeConflict("owned delete accounting mismatch")
		}
		if err = r.deleteMetadata(ctx, q, item); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "DELETE FROM cluster_upload_sessions WHERE storage_member_id=? AND media_id=? AND state='complete'", row.ID, item.OwnedID); err != nil {
			return err
		}
		now := time.Now().Unix()
		if _, err = q.ExecContext(ctx, "UPDATE cluster_storage_members SET used_bytes=used_bytes-?,physical_free_bytes=?,updated_at=? WHERE member_id=?", item.Bytes, max(0, free), now, row.ID); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE media_delete_operations SET state='committed' WHERE operation_id=?", id); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "storage-delete-committed", "", map[string]any{"operation": id, "object_id": item.OwnedID, "bytes": item.Bytes}, a)
	})
}
