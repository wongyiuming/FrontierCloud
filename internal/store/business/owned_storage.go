package business

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (r *Repository) ownedStorageNode(ctx context.Context, q queryer, relationship string) (store.NodeIdentity, error) {
	row, err := readNode(ctx, q, r.lock())
	if err != nil {
		return row, err
	}
	if row.Role != "Follower" {
		return row, nodeConflict("only Follower accepts owned storage uploads")
	}
	if relationship != "" {
		rel, err := scanRelation(q.QueryRowContext(ctx, "SELECT "+relationColumns+" FROM node_relationships WHERE relationship_id=?"+r.lock(), relationship))
		if err != nil {
			return row, err
		}
		if rel.State != "active" || rel.Direction != "upstream" || rel.Protocol != 2 {
			return row, nodeConflict("owned storage relationship is not active")
		}
	}
	return row, nil
}
func (r *Repository) ReserveOwnedUpload(ctx context.Context, operation, relationship string, o store.MediaObject, size, free int64, a store.NodeAudit) error {
	kind := o.Kind
	if !poolPath(store.LocalMedia{MediaObject: o, Bytes: size}) || !nodeIDPattern.MatchString(operation) || !nodeIDPattern.MatchString(relationship) || !nodeHashPattern.MatchString(o.ID) || size <= 0 || size > 10*store.GiB {
		return nodeConflict("invalid owned upload reservation")
	}
	return r.write(ctx, func(q queryer) error {
		row, err := r.ownedStorageNode(ctx, q, relationship)
		if err != nil {
			return err
		}
		member, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), row.ID))
		if errors.Is(err, sql.ErrNoRows) {
			return store.ErrStorageCapacity
		}
		if err != nil {
			return err
		}
		if member.Enabled != 1 || member.Writable != 1 || min(member.Allocation-member.Used-member.Reserved, max(0, free-store.PhysicalReserve-member.Reserved)) < size {
			return store.ErrStorageCapacity
		}
		var count int
		if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM media_objects WHERE media_id=? OR path_locator=?", o.ID, locator(o.Path)).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return nodeConflict("owned storage target already exists")
		}
		if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM cluster_upload_sessions WHERE media_id=? OR path_locator=? OR upload_id=?", o.ID, locator(o.Path), operation).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return nodeConflict("owned storage upload already reserved")
		}
		now := time.Now().Unix()
		if _, err = q.ExecContext(ctx, "INSERT INTO cluster_upload_sessions(upload_id,storage_member_id,media_id,media_path,path_locator,object_kind,expected_bytes,state,expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'reserved',?,?,?)", operation, row.ID, o.ID, o.Path, locator(o.Path), kind, size, now+1800, now, now); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_storage_members SET reserved_bytes=reserved_bytes+?,physical_free_bytes=?,updated_at=? WHERE member_id=?", size, max(0, free), now, row.ID); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "storage-upload-reserved", relationship, map[string]any{"operation": operation, "object_id": o.ID, "path": o.Path, "bytes": size}, a)
	})
}
func (r *Repository) CompleteOwnedUpload(ctx context.Context, operation string, o store.MediaObject, size int64, etag string, free int64, a store.NodeAudit) error {
	if !nodeIDPattern.MatchString(operation) || !nodeHashPattern.MatchString(o.ID) {
		return nodeConflict("invalid owned upload publication")
	}
	kind := o.Kind
	if !poolPath(store.LocalMedia{MediaObject: o, Bytes: size, ETag: etag}) || size <= 0 {
		return nodeConflict("invalid owned upload publication")
	}
	return r.write(ctx, func(q queryer) error {
		row, err := r.ownedStorageNode(ctx, q, "")
		if err != nil {
			return err
		}
		session, err := scanUpload(q.QueryRowContext(ctx, "SELECT "+uploadColumns+" FROM cluster_upload_sessions WHERE upload_id=?"+r.lock(), operation))
		if err != nil {
			return err
		}
		if session.MemberID != row.ID || session.MediaID != o.ID || session.Path != o.Path || session.Kind != kind || session.ExpectedBytes != size {
			return nodeConflict("owned publication differs from reservation")
		}
		var objectID, objectPath, objectKind string
		err = q.QueryRowContext(ctx, "SELECT media_id,media_path,object_kind FROM media_objects WHERE media_id=? OR path_locator=?"+r.lock(), o.ID, locator(o.Path)).Scan(&objectID, &objectPath, &objectKind)
		if session.State == "complete" {
			if err != nil {
				return err
			}
			if objectID != o.ID || objectPath != o.Path || objectKind != kind {
				return nodeConflict("owned publication replay conflict")
			}
			return nil
		}
		if session.State != "reserved" {
			return nodeConflict("owned publication reservation is not live")
		}
		if err == nil {
			return nodeConflict("owned publication target occupied")
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		member, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), row.ID))
		if err != nil {
			return err
		}
		if member.Reserved < size {
			return nodeConflict("owned publication reservation accounting mismatch")
		}
		now := time.Now()
		stamp := timestamp(now)
		if _, err = q.ExecContext(ctx, "INSERT INTO media_objects(media_id,object_kind,media_path,path_locator,created_at,updated_at) VALUES (?,?,?,?,?,?)", o.ID, kind, o.Path, locator(o.Path), stamp, stamp); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_storage_members SET reserved_bytes=reserved_bytes-?,used_bytes=used_bytes+?,physical_free_bytes=?,updated_at=? WHERE member_id=?", size, size, max(0, free), now.Unix(), row.ID); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_upload_sessions SET state='complete',path_locator=NULL,updated_at=? WHERE upload_id=?", now.Unix(), operation); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "storage-upload-published", "", map[string]any{"operation": operation, "object_id": o.ID, "path": o.Path, "bytes": size, "etag": etag}, a)
	})
}

// The caller must first prove the operation's private stage is not live and
// remove it. A timeout/expired capability alone never releases disk capacity.
func (r *Repository) ReleaseOwnedUpload(ctx context.Context, operation string, a store.NodeAudit) error {
	if !nodeIDPattern.MatchString(operation) {
		return nodeConflict("invalid owned upload operation")
	}
	return r.write(ctx, func(q queryer) error {
		row, err := r.ownedStorageNode(ctx, q, "")
		if err != nil {
			return err
		}
		v, err := scanUpload(q.QueryRowContext(ctx, "SELECT "+uploadColumns+" FROM cluster_upload_sessions WHERE upload_id=?"+r.lock(), operation))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if v.MemberID != row.ID || v.State != "reserved" {
			return nodeConflict("owned upload cannot be discarded")
		}
		var occupied int
		if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM media_objects WHERE media_id=? OR path_locator=?", v.MediaID, locator(v.Path)).Scan(&occupied); err != nil {
			return err
		}
		if occupied != 0 {
			return nodeConflict("owned upload has published bytes")
		}
		var reserved int64
		if err = q.QueryRowContext(ctx, "SELECT reserved_bytes FROM cluster_storage_members WHERE member_id=?"+r.lock(), row.ID).Scan(&reserved); err != nil {
			return err
		}
		if reserved < v.ExpectedBytes {
			return nodeConflict("owned reservation accounting mismatch")
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_storage_members SET reserved_bytes=reserved_bytes-?,updated_at=? WHERE member_id=?", v.ExpectedBytes, time.Now().Unix(), row.ID); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "DELETE FROM cluster_upload_sessions WHERE upload_id=?", operation); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "storage-upload-cleaned", "", map[string]any{"operation": operation, "object_id": v.MediaID}, a)
	})
}
func (r *Repository) OwnedPendingUploads(ctx context.Context) ([]store.UploadReservation, error) {
	node, err := r.ReadIdentity(ctx)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if node.Role != "Follower" {
		return nil, nil
	}
	rows, err := r.db.QueryContext(ctx, "SELECT "+uploadColumns+" FROM cluster_upload_sessions WHERE storage_member_id=? AND state='reserved' ORDER BY upload_id", node.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []store.UploadReservation{}
	for rows.Next() {
		v, err := scanUpload(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, v)
	}
	return result, rows.Err()
}
