package business

import (
	"context"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"time"
)

func (r *Repository) CheckLocalRecordingUpload(ctx context.Context, v store.Recording) error {
	return r.write(ctx, func(q queryer) error {
		n, e := readNode(ctx, q, r.lock())
		if e != nil {
			return e
		}
		if n.Role != "Master" || n.ID != v.MemberID {
			return store.ErrRecordingState
		}
		var status string
		if e = q.QueryRowContext(ctx, "SELECT status FROM karaoke_users WHERE user_id=?"+r.lock(), v.UserID).Scan(&status); e != nil {
			return e
		}
		if status != "active" {
			return store.ErrUserBlocked
		}
		current, e := scanRecording(q.QueryRowContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE recording_id=?"+r.lock(), v.ID))
		if e != nil {
			return e
		}
		if current.UserID != v.UserID || current.MemberID != v.MemberID || current.Bytes != v.Bytes || current.Filename != v.Filename || current.ContentType != v.ContentType || (current.State != "pending" && current.State != "ready") {
			return store.ErrRecordingState
		}
		return nil
	})
}
func (r *Repository) StageOwnedRecordingDeletion(ctx context.Context, relationship, user, id string) (result *store.Recording, err error) {
	if !nodeIDPattern.MatchString(user) || !nodeIDPattern.MatchString(id) {
		return nil, store.ErrRecordingState
	}
	err = r.write(ctx, func(q queryer) error {
		n, e := r.ownedStorageNode(ctx, q, relationship)
		if e != nil {
			return e
		}
		v, e := recordingLookup(q.QueryRowContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE recording_id=?"+r.lock(), id))
		if e != nil {
			return e
		}
		if v == nil {
			// Even a never-uploaded Direct ticket can remain valid in a browser.
			// Persist an ownership tombstone before acknowledging its cancellation.
			now := time.Now().Unix()
			_, e = q.ExecContext(ctx, "INSERT INTO karaoke_recordings("+recordingColumns+") VALUES (?,?,?,?,'application/octet-stream',0,NULL,'deleted','','[]',?,?)", id, user, n.ID, id+".bin", now, now)
			if e != nil {
				return e
			}
			result = &store.Recording{ID: id, UserID: user, MemberID: n.ID, State: "deleted"}
			return nil
		}
		if v.UserID != user || v.MemberID != n.ID {
			return store.ErrRecordingState
		}
		result = v
		if v.State == "deleted" || v.State == "deleting" {
			return nil
		}
		if v.State != "pending" && v.State != "ready" {
			return store.ErrRecordingState
		}
		_, e = q.ExecContext(ctx, "UPDATE karaoke_recordings SET state='deleting',updated_at=? WHERE recording_id=?", time.Now().Unix(), id)
		result.State = "deleting"
		return e
	})
	return
}

func (r *Repository) ReserveOwnedRecording(ctx context.Context, relationship string, v store.Recording, free int64, a store.NodeAudit) error {
	if !nodeIDPattern.MatchString(relationship) || !validRecording(v) || free < 0 {
		return store.ErrRecordingState
	}
	return r.write(ctx, func(q queryer) error {
		n, e := r.ownedStorageNode(ctx, q, relationship)
		if e != nil {
			return e
		}
		var blocked int
		if e = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM karaoke_users WHERE user_id=? AND status IN ('deleting','deleted')", v.UserID).Scan(&blocked); e != nil {
			return e
		}
		if blocked != 0 {
			return store.ErrRecordingState
		}
		old, e := recordingLookup(q.QueryRowContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE recording_id=?"+r.lock(), v.ID))
		if e != nil {
			return e
		}
		if old != nil {
			if old.MemberID != n.ID || old.UserID != v.UserID || old.Bytes != v.Bytes || old.Filename != v.Filename || old.ContentType != v.ContentType || (old.State != "pending" && old.State != "ready") {
				return store.ErrRecordingState
			}
			return nil
		}
		m, e := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), n.ID))
		if e != nil {
			return e
		}
		m.PhysicalFree = free
		if capacity(m) < v.Bytes {
			return store.ErrStorageCapacity
		}
		v.MemberID = n.ID
		v.CreatedAt = time.Now().Unix()
		v.UpdatedAt = v.CreatedAt
		if e = insertRecording(ctx, q, v); e != nil {
			return e
		}
		if _, e = q.ExecContext(ctx, "UPDATE cluster_storage_members SET reserved_bytes=reserved_bytes+?,physical_free_bytes=?,updated_at=? WHERE member_id=?", v.Bytes, free, v.UpdatedAt, n.ID); e != nil {
			return e
		}
		return r.nodeAudit(ctx, q, "recording-upload-reserved", relationship, map[string]any{"recording_id": v.ID, "user_id": v.UserID, "size_bytes": v.Bytes}, a)
	})
}
func (r *Repository) StageOwnedUserDeletion(ctx context.Context, relationship, user string) error {
	if !nodeIDPattern.MatchString(user) {
		return store.ErrRecordingState
	}
	return r.write(ctx, func(q queryer) error {
		if _, e := r.ownedStorageNode(ctx, q, relationship); e != nil {
			return e
		}
		now := time.Now().Unix()
		// A Follower has no interactive account service. This private ownership
		// tombstone blocks capabilities issued before the Master's account deletion.
		if _, e := q.ExecContext(ctx, r.ignoreInsert()+" INTO karaoke_users("+userColumns+") VALUES (?,?,?,'!','deleting',0,0,?,?)", user, user, user, now, now); e != nil {
			return e
		}
		if _, e := q.ExecContext(ctx, "UPDATE karaoke_users SET status='deleting',updated_at=? WHERE user_id=?", now, user); e != nil {
			return e
		}
		_, e := q.ExecContext(ctx, "UPDATE karaoke_recordings SET state='deleting',updated_at=? WHERE user_id=? AND state IN ('pending','ready')", now, user)
		return e
	})
}
func (r *Repository) CompleteOwnedRecording(ctx context.Context, relationship string, receipt store.RecordingReceipt, free int64, a store.NodeAudit) error {
	if !validRecordingReceipt(receipt) || free < 0 {
		return store.ErrRecordingState
	}
	return r.write(ctx, func(q queryer) error {
		n, e := r.ownedStorageNode(ctx, q, relationship)
		if e != nil {
			return e
		}
		v, e := scanRecording(q.QueryRowContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE recording_id=?"+r.lock(), receipt.ID))
		if e != nil {
			return e
		}
		if v.MemberID != n.ID || v.Bytes != receipt.Bytes {
			return store.ErrRecordingState
		}
		if v.State == "ready" {
			if v.SHA256 == nil || *v.SHA256 != receipt.SHA256 {
				return store.ErrRecordingState
			}
			return nil
		}
		if v.State != "pending" {
			return store.ErrRecordingState
		}
		m, e := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), n.ID))
		if e != nil {
			return e
		}
		if m.Reserved < v.Bytes {
			return store.ErrRecordingState
		}
		now := time.Now().Unix()
		if _, e = q.ExecContext(ctx, "UPDATE karaoke_recordings SET state='ready',sha256=?,updated_at=? WHERE recording_id=?", receipt.SHA256, now, v.ID); e != nil {
			return e
		}
		if _, e = q.ExecContext(ctx, "UPDATE cluster_storage_members SET reserved_bytes=reserved_bytes-?,used_bytes=used_bytes+?,physical_free_bytes=?,updated_at=? WHERE member_id=?", v.Bytes, v.Bytes, free, now, n.ID); e != nil {
			return e
		}
		return r.nodeAudit(ctx, q, "recording-upload-published", relationship, map[string]any{"recording_id": v.ID, "user_id": v.UserID, "size_bytes": v.Bytes, "sha256": receipt.SHA256}, a)
	})
}
func (r *Repository) CompleteOwnedRecordingDeletion(ctx context.Context, relationship, user, id string, free int64, a store.NodeAudit) error {
	if !nodeIDPattern.MatchString(relationship) || !nodeIDPattern.MatchString(user) || !nodeIDPattern.MatchString(id) || free < 0 {
		return store.ErrRecordingState
	}
	return r.write(ctx, func(q queryer) error {
		n, e := r.ownedStorageNode(ctx, q, relationship)
		if e != nil {
			return e
		}
		v, e := recordingLookup(q.QueryRowContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE recording_id=?"+r.lock(), id))
		if e != nil {
			return e
		}
		if v == nil {
			return nil
		}
		if v.UserID != user || v.MemberID != n.ID {
			return store.ErrRecordingState
		}
		if v.State == "deleted" {
			return nil
		}
		if v.State != "deleting" {
			return store.ErrRecordingState
		}
		m, e := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), n.ID))
		if e != nil {
			return e
		}
		column := "reserved_bytes"
		if v.SHA256 != nil {
			column = "used_bytes"
			if m.Used < v.Bytes {
				return store.ErrRecordingState
			}
		} else if m.Reserved < v.Bytes {
			return store.ErrRecordingState
		}
		now := time.Now().Unix()
		if _, e = q.ExecContext(ctx, "UPDATE cluster_storage_members SET "+column+"="+column+"-?,physical_free_bytes=?,updated_at=? WHERE member_id=?", v.Bytes, free, now, n.ID); e != nil {
			return e
		}
		// Keep an ownership tombstone. A still-valid upload capability must not
		// recreate a recording whose physical deletion was already acknowledged.
		if _, e = q.ExecContext(ctx, "UPDATE karaoke_recordings SET state='deleted',updated_at=? WHERE recording_id=?", now, id); e != nil {
			return e
		}
		return r.nodeAudit(ctx, q, "recording-deleted", relationship, map[string]any{"recording_id": id, "user_id": user, "size_bytes": v.Bytes}, a)
	})
}
