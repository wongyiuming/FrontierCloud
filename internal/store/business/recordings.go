package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"sort"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

const recordingColumns = "recording_id,user_id,storage_member_id,filename,content_type,size_bytes,sha256,state,title,lyrics,created_at,updated_at"

func scanRecording(row rowScanner) (v store.Recording, err error) {
	var lyrics []byte
	err = row.Scan(&v.ID, &v.UserID, &v.MemberID, &v.Filename, &v.ContentType, &v.Bytes, &v.SHA256, &v.State, &v.Title, &lyrics, &v.CreatedAt, &v.UpdatedAt)
	if err == nil {
		err = json.Unmarshal(lyrics, &v.Lyrics)
		if v.Lyrics == nil {
			v.Lyrics = []store.RecordingLyric{}
		}
	}
	return
}
func recordingLookup(row rowScanner) (*store.Recording, error) {
	v, err := scanRecording(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &v, nil
}
func (r *Repository) Recording(ctx context.Context, id string) (*store.Recording, error) {
	if !nodeIDPattern.MatchString(id) {
		return nil, nil
	}
	return recordingLookup(r.db.QueryRowContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE recording_id=?", id))
}
func (r *Repository) ListRecordings(ctx context.Context, user, state string, limit int) ([]store.Recording, error) {
	if !nodeIDPattern.MatchString(user) || (state != "ready" && state != "deleting" && state != "pending") {
		return nil, store.ErrRecordingState
	}
	limit = min(500, max(1, limit))
	rows, err := r.db.QueryContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE user_id=? AND state=? ORDER BY created_at DESC,recording_id LIMIT ?", user, state, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	values := []store.Recording{}
	for rows.Next() {
		v, e := scanRecording(rows)
		if e != nil {
			return nil, e
		}
		values = append(values, v)
	}
	return values, rows.Err()
}
func validRecording(v store.Recording) bool {
	return nodeIDPattern.MatchString(v.ID) && nodeIDPattern.MatchString(v.UserID) && v.Bytes > 0 && v.Bytes <= store.MaxRecordingBytes && store.ValidRecordingFilename(v.Filename) && store.RecordingContentType(v.ContentType) && store.ValidRecordingMetadata(store.RecordingMetadata{Title: v.Title, Lyrics: v.Lyrics})
}
func insertRecording(ctx context.Context, q queryer, v store.Recording) error {
	if v.Lyrics == nil {
		v.Lyrics = []store.RecordingLyric{}
	}
	lyrics, err := json.Marshal(v.Lyrics)
	if err != nil {
		return err
	}
	_, err = q.ExecContext(ctx, "INSERT INTO karaoke_recordings("+recordingColumns+") VALUES (?,?,?,?,?,?,NULL,'pending',?,?,?,?)", v.ID, v.UserID, v.MemberID, v.Filename, v.ContentType, v.Bytes, v.Title, string(lyrics), v.CreatedAt, v.UpdatedAt)
	return err
}
func (r *Repository) ReserveRecording(ctx context.Context, v store.Recording, free int64, a store.KaraokeAudit) (result store.Recording, member store.StorageMember, err error) {
	if !validRecording(v) || free < 0 {
		return result, member, store.ErrRecordingState
	}
	err = r.write(ctx, func(q queryer) error {
		node, e := readNode(ctx, q, r.lock())
		if e != nil {
			return e
		}
		if node.Role != "Master" {
			return store.ErrNodeState
		}
		u, e := scanUser(q.QueryRowContext(ctx, "SELECT "+userColumns+" FROM karaoke_users WHERE user_id=?"+r.lock(), v.UserID))
		if errors.Is(e, sql.ErrNoRows) {
			return store.ErrUserMissing
		}
		if e != nil {
			return e
		}
		if u.Status != "active" {
			return store.ErrUserBlocked
		}
		if u.Used < 0 || u.Quota-u.Used < v.Bytes {
			return store.ErrRecordingQuota
		}
		rows, e := q.QueryContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members ORDER BY member_id"+r.lock())
		if e != nil {
			return e
		}
		members := []store.StorageMember{}
		for rows.Next() {
			m, e := scanMember(rows)
			if e != nil {
				rows.Close()
				return e
			}
			members = append(members, m)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		candidates := []store.StorageMember{}
		now := time.Now().Unix()
		for _, m := range members {
			if m.ID == node.ID && m.Kind == "MasterLocal" {
				m.PhysicalFree = free
			} else {
				if m.Kind != "Follower" || m.RelationshipID == nil {
					continue
				}
				rel, e := scanRelation(q.QueryRowContext(ctx, "SELECT "+relationColumns+" FROM node_relationships WHERE relationship_id=?"+r.lock(), *m.RelationshipID))
				if e != nil {
					return e
				}
				if rel.State != "active" || rel.Direction != "downstream" || rel.PeerID != m.ID || rel.Status != "online" || now-rel.LastHeartbeat >= 120 {
					continue
				}
			}
			m.Available = capacity(m)
			if m.Available >= v.Bytes {
				candidates = append(candidates, m)
			}
		}
		if len(candidates) == 0 {
			return store.ErrStorageCapacity
		}
		sort.Slice(candidates, func(i, j int) bool {
			x, y := candidates[i], candidates[j]
			px, py := float64(x.Used+x.Reserved)/float64(max(int64(1), x.Allocation)), float64(y.Used+y.Reserved)/float64(max(int64(1), y.Allocation))
			if px != py {
				return px < py
			}
			if x.Available != y.Available {
				return x.Available > y.Available
			}
			return x.ID < y.ID
		})
		member = candidates[0]
		result = v
		result.MemberID = member.ID
		result.State = "pending"
		result.SHA256 = nil
		result.CreatedAt, result.UpdatedAt = now, now
		if e = insertRecording(ctx, q, result); e != nil {
			return e
		}
		if _, e = q.ExecContext(ctx, "UPDATE karaoke_users SET used_bytes=used_bytes+?,updated_at=? WHERE user_id=?", v.Bytes, now, v.UserID); e != nil {
			return e
		}
		if _, e = q.ExecContext(ctx, "UPDATE cluster_storage_members SET reserved_bytes=reserved_bytes+?,physical_free_bytes=?,updated_at=? WHERE member_id=?", v.Bytes, member.PhysicalFree, now, member.ID); e != nil {
			return e
		}
		a.UserID, a.Action, a.Result = v.UserID, "recording-reserve", "success"
		a.Detail = map[string]any{"recording_id": v.ID, "size_bytes": v.Bytes, "storage_member_id": member.ID}
		return r.karaokeAudit(ctx, q, a)
	})
	return
}
func validRecordingReceipt(v store.RecordingReceipt) bool {
	return nodeIDPattern.MatchString(v.ID) && v.Bytes > 0 && v.Bytes <= store.MaxRecordingBytes && nodeHashPattern.MatchString(v.SHA256) && (v.Metadata == nil || store.ValidRecordingMetadata(*v.Metadata))
}
func (r *Repository) FinalizeRecording(ctx context.Context, user, id string, receipt store.RecordingReceipt, a store.KaraokeAudit) error {
	if id != receipt.ID || !validRecordingReceipt(receipt) {
		return store.ErrRecordingState
	}
	return r.write(ctx, func(q queryer) error {
		if e := r.accountMaster(ctx, q); e != nil {
			return e
		}
		u, e := scanUser(q.QueryRowContext(ctx, "SELECT "+userColumns+" FROM karaoke_users WHERE user_id=?"+r.lock(), user))
		if e != nil {
			return e
		}
		if u.Status != "active" {
			return store.ErrUserBlocked
		}
		v, e := scanRecording(q.QueryRowContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE recording_id=?"+r.lock(), id))
		if errors.Is(e, sql.ErrNoRows) {
			return store.ErrRecordingMissing
		}
		if e != nil {
			return e
		}
		if v.UserID != user {
			return store.ErrRecordingMissing
		}
		if v.State == "ready" {
			if v.Bytes != receipt.Bytes || v.SHA256 == nil || *v.SHA256 != receipt.SHA256 {
				return store.ErrRecordingState
			}
			return nil
		}
		if v.State != "pending" || receipt.Bytes > v.Bytes || u.Used < v.Bytes {
			return store.ErrRecordingState
		}
		m, e := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), v.MemberID))
		if e != nil {
			return e
		}
		if m.Reserved < v.Bytes {
			return store.ErrRecordingState
		}
		now := time.Now().Unix()
		if receipt.Metadata != nil {
			if receipt.Metadata.Title != "" {
				v.Title = receipt.Metadata.Title
			}
			if receipt.Metadata.Lyrics != nil {
				v.Lyrics = receipt.Metadata.Lyrics
			}
		}
		if v.Lyrics == nil {
			v.Lyrics = []store.RecordingLyric{}
		}
		lyrics, e := json.Marshal(v.Lyrics)
		if e != nil {
			return e
		}
		if _, e = q.ExecContext(ctx, "UPDATE karaoke_users SET used_bytes=used_bytes-?,updated_at=? WHERE user_id=?", v.Bytes-receipt.Bytes, now, user); e != nil {
			return e
		}
		if _, e = q.ExecContext(ctx, "UPDATE cluster_storage_members SET reserved_bytes=reserved_bytes-?,used_bytes=used_bytes+?,updated_at=? WHERE member_id=?", v.Bytes, receipt.Bytes, now, v.MemberID); e != nil {
			return e
		}
		if _, e = q.ExecContext(ctx, "UPDATE karaoke_recordings SET state='ready',size_bytes=?,sha256=?,title=?,lyrics=?,updated_at=? WHERE recording_id=?", receipt.Bytes, receipt.SHA256, v.Title, string(lyrics), now, id); e != nil {
			return e
		}
		a.UserID, a.Action, a.Result = user, "recording-finalize", "success"
		a.Detail = map[string]any{"recording_id": id, "size_bytes": receipt.Bytes, "sha256": receipt.SHA256}
		return r.karaokeAudit(ctx, q, a)
	})
}
func (r *Repository) StageRecordingDeletion(ctx context.Context, user, id string, pendingOnly bool, a store.KaraokeAudit) (result *store.Recording, err error) {
	if !nodeIDPattern.MatchString(user) || !nodeIDPattern.MatchString(id) {
		return nil, store.ErrRecordingMissing
	}
	err = r.write(ctx, func(q queryer) error {
		if e := r.accountMaster(ctx, q); e != nil {
			return e
		}
		v, e := recordingLookup(q.QueryRowContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE recording_id=?"+r.lock(), id))
		if e != nil {
			return e
		}
		if v == nil {
			result = nil
			return nil
		}
		if v.UserID != user {
			return store.ErrRecordingMissing
		}
		if pendingOnly && (v.State == "ready" || v.SHA256 != nil) {
			return store.ErrRecordingState
		}
		if v.State != "pending" && v.State != "ready" && v.State != "deleting" {
			return store.ErrRecordingState
		}
		result = v
		if v.State == "deleting" {
			return nil
		}
		result.State = "deleting"
		if _, e = q.ExecContext(ctx, "UPDATE karaoke_recordings SET state='deleting',updated_at=? WHERE recording_id=?", time.Now().Unix(), id); e != nil {
			return e
		}
		a.UserID, a.Result = user, "pending"
		if a.Action == "" {
			a.Action = "recording-delete"
		}
		a.Detail = map[string]any{"recording_id": id}
		return r.karaokeAudit(ctx, q, a)
	})
	return
}
func (r *Repository) CompleteRecordingDeletion(ctx context.Context, user, id string, a store.KaraokeAudit) error {
	return r.write(ctx, func(q queryer) error {
		if e := r.accountMaster(ctx, q); e != nil {
			return e
		}
		v, e := recordingLookup(q.QueryRowContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE recording_id=?"+r.lock(), id))
		if e != nil || v == nil {
			return e
		}
		if v.UserID != user || v.State != "deleting" {
			return store.ErrRecordingState
		}
		var used int64
		if e = q.QueryRowContext(ctx, "SELECT used_bytes FROM karaoke_users WHERE user_id=?"+r.lock(), user).Scan(&used); e != nil {
			return e
		}
		if used < v.Bytes {
			return store.ErrRecordingState
		}
		m, e := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), v.MemberID))
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
		if _, e = q.ExecContext(ctx, "UPDATE cluster_storage_members SET "+column+"="+column+"-?,updated_at=? WHERE member_id=?", v.Bytes, now, v.MemberID); e != nil {
			return e
		}
		if _, e = q.ExecContext(ctx, "UPDATE karaoke_users SET used_bytes=used_bytes-?,updated_at=? WHERE user_id=?", v.Bytes, now, user); e != nil {
			return e
		}
		if _, e = q.ExecContext(ctx, "DELETE FROM karaoke_recordings WHERE recording_id=?", id); e != nil {
			return e
		}
		a.UserID, a.Result = user, "success"
		if a.Action == "" {
			a.Action = "recording-delete"
		}
		a.Detail = map[string]any{"recording_id": id, "size_bytes": v.Bytes}
		return r.karaokeAudit(ctx, q, a)
	})
}
func (r *Repository) PendingRecordingDeletions(ctx context.Context, limit int) ([]store.Recording, error) {
	rows, e := r.db.QueryContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE state='deleting' ORDER BY updated_at,recording_id LIMIT ?", min(500, max(1, limit)))
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	values := []store.Recording{}
	for rows.Next() {
		v, e := scanRecording(rows)
		if e != nil {
			return nil, e
		}
		values = append(values, v)
	}
	return values, rows.Err()
}
func (r *Repository) DeferRecordingDeletion(ctx context.Context, id string) error {
	_, e := r.db.ExecContext(ctx, "UPDATE karaoke_recordings SET updated_at=? WHERE recording_id=? AND state='deleting'", time.Now().Unix(), id)
	return e
}
func (r *Repository) StageExpiredRecordings(ctx context.Context, limit int) error {
	return r.write(ctx, func(q queryer) error {
		if e := r.accountMaster(ctx, q); e != nil {
			return e
		}
		now := time.Now().Unix()
		rows, e := q.QueryContext(ctx, "SELECT recording_id FROM karaoke_recordings WHERE state='pending' AND updated_at<=? ORDER BY updated_at,recording_id LIMIT ?"+r.lock(), now-3600, min(100, max(1, limit)))
		if e != nil {
			return e
		}
		ids := []string{}
		for rows.Next() {
			var id string
			if e = rows.Scan(&id); e != nil {
				rows.Close()
				return e
			}
			ids = append(ids, id)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		for _, id := range ids {
			if _, e = q.ExecContext(ctx, "UPDATE karaoke_recordings SET state='deleting',updated_at=? WHERE recording_id=?", now, id); e != nil {
				return e
			}
		}
		return nil
	})
}

var _ store.RecordingRepository = (*Repository)(nil)
