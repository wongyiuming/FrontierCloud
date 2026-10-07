package business

import (
	"context"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"github.com/wongyiuming/FrontierCloud/migrations"
	"time"
)

func (r *Repository) recordingAdoptionIdle(ctx context.Context, q queryer) error {
	var generation int
	if err := q.QueryRowContext(ctx, "SELECT generation FROM frontiercloud_schema WHERE singleton=1"+r.lock()).Scan(&generation); err != nil {
		return err
	}
	if generation != migrations.Generation {
		return store.ErrBackupState
	}
	for _, query := range []string{
		"SELECT COUNT(*) FROM karaoke_recordings WHERE state NOT IN ('ready','deleted')",
		"SELECT COUNT(*) FROM karaoke_users WHERE status NOT IN ('active','banned','deleted')",
		"SELECT COUNT(*) FROM cluster_storage_members WHERE reserved_bytes<>0",
		"SELECT COUNT(*) FROM cluster_upload_sessions WHERE state NOT IN ('complete','cancelled')",
		"SELECT COUNT(*) FROM media_delete_operations WHERE state<>'rename_done'",
		"SELECT COUNT(*) FROM global_media_objects WHERE state<>'active'",
		"SELECT COUNT(*) FROM cluster_business_backups WHERE state NOT IN ('ready','failed')",
		"SELECT COUNT(*) FROM cluster_worker_jobs WHERE state NOT IN ('queued','complete')",
	} {
		var count int64
		if err := q.QueryRowContext(ctx, query).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return store.ErrBackupBusy
		}
	}
	return nil
}

func (r *Repository) ExportRecordingInventory(ctx context.Context, relationship string, now int64) (result store.RecordingInventory, err error) {
	if !nodeIDPattern.MatchString(relationship) || now <= 0 || now > 1<<62 {
		return result, store.ErrNodeState
	}
	err = r.write(ctx, func(q queryer) error {
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		rel, err := scanRelation(q.QueryRowContext(ctx, "SELECT "+relationColumns+" FROM node_relationships WHERE relationship_id=?"+r.lock(), relationship))
		if err != nil {
			return err
		}
		if n.Role != "Master" || rel.Direction != "downstream" || rel.State != "active" || rel.Protocol != 2 {
			return store.ErrNodeState
		}
		member, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), rel.PeerID))
		if err != nil {
			return err
		}
		if member.Kind != "Follower" || member.RelationshipID == nil || *member.RelationshipID != rel.ID {
			return store.ErrNodeState
		}
		if err := r.recordingAdoptionIdle(ctx, q); err != nil {
			return err
		}
		result = store.RecordingInventory{Kind: "frontiercloud-recording-inventory", Version: 1, SchemaGeneration: migrations.Generation, MasterID: n.ID, FollowerID: rel.PeerID, Relationship: rel.ID, CreatedAt: now, ExpiresAt: now + 1800, Recordings: []store.RecordingProof{}}
		rows, err := q.QueryContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings WHERE storage_member_id=? AND state='ready' ORDER BY recording_id LIMIT 5001"+r.lock(), rel.PeerID)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			v, err := scanRecording(rows)
			if err != nil {
				return err
			}
			if v.SHA256 == nil || !validRecording(v) || !nodeHashPattern.MatchString(*v.SHA256) || v.CreatedAt <= 0 {
				return store.ErrRecordingState
			}
			result.Recordings = append(result.Recordings, store.RecordingProof{ID: v.ID, UserID: v.UserID, Filename: v.Filename, ContentType: v.ContentType, Bytes: v.Bytes, SHA256: *v.SHA256, CreatedAt: v.CreatedAt})
		}
		if err := rows.Err(); err != nil {
			return err
		}
		if len(result.Recordings) > store.MaxRecordingInventoryItems {
			return store.ErrRecordingState
		}
		return nil
	})
	if err != nil {
		return store.RecordingInventory{}, err
	}
	return result, nil
}

// A native offline volume lease and a complete physical hash scan are required
// by the caller. Current pinned authority is reverified inside the transaction.
// Existing quotas, user accounts, IDs and tombstones are never rewritten.
func (r *Repository) AdoptOwnedRecordings(ctx context.Context, signed store.SignedRecordingInventory, confirmation string, a store.NodeAudit) (adopted int, err error) {
	v := signed.Payload
	now := time.Now().Unix()
	if !nodeIDPattern.MatchString(confirmation) || v.Kind != "frontiercloud-recording-inventory" || v.Version != 1 || v.SchemaGeneration != migrations.Generation || v.FollowerID != confirmation || !nodeIDPattern.MatchString(v.MasterID) || !nodeIDPattern.MatchString(v.Relationship) || v.CreatedAt <= 0 || v.CreatedAt > now+30 || v.ExpiresAt != v.CreatedAt+1800 || v.ExpiresAt <= now || len(v.Recordings) > store.MaxRecordingInventoryItems || v.Recordings == nil {
		return 0, store.ErrNodeState
	}
	payload, err := v.CanonicalPayload()
	if err != nil {
		return 0, store.ErrNodeState
	}
	byID := map[string]store.RecordingProof{}
	var bytes int64
	for _, proof := range v.Recordings {
		row := store.Recording{ID: proof.ID, UserID: proof.UserID, Filename: proof.Filename, ContentType: proof.ContentType, Bytes: proof.Bytes}
		if !validRecording(row) || !nodeHashPattern.MatchString(proof.SHA256) || proof.CreatedAt <= 0 || proof.CreatedAt > v.CreatedAt || bytes > store.MaxStorageAllocation-proof.Bytes {
			return 0, store.ErrRecordingState
		}
		if _, ok := byID[proof.ID]; ok {
			return 0, store.ErrRecordingState
		}
		byID[proof.ID] = proof
		bytes += proof.Bytes
	}
	err = r.write(ctx, func(q queryer) error {
		adopted = 0
		n, rel, err := r.backupUpstream(ctx, q, v.Relationship)
		if err != nil {
			return err
		}
		if n.ID != confirmation || rel.PeerID != v.MasterID || protocol.Verify(rel.PublicKey, payload, signed.Signature) != nil {
			return store.ErrNodeState
		}
		if err := r.recordingAdoptionIdle(ctx, q); err != nil {
			return err
		}
		member, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), n.ID))
		if err != nil {
			return err
		}
		if member.Kind != "Follower" || member.Transport != "Local" || member.RelationshipID != nil || member.Reserved != 0 || member.Used < bytes || member.Used > member.Allocation {
			return store.ErrRecordingState
		}
		// Require exact existing audio/video receipts first. Unexplained used bytes
		// are not authority for fabricating additional recording ownership.
		var mediaBytes int64
		if err = q.QueryRowContext(ctx, "SELECT COALESCE(SUM(expected_bytes),0) FROM cluster_upload_sessions WHERE storage_member_id=? AND state='complete'", n.ID).Scan(&mediaBytes); err != nil {
			return err
		}
		if mediaBytes < 0 || mediaBytes > store.MaxStorageAllocation-bytes || member.Used != mediaBytes+bytes {
			return store.ErrRecordingState
		}
		rows, err := q.QueryContext(ctx, "SELECT "+recordingColumns+" FROM karaoke_recordings ORDER BY recording_id LIMIT 5001"+r.lock())
		if err != nil {
			return err
		}
		existing := map[string]store.Recording{}
		for rows.Next() {
			row, e := scanRecording(rows)
			if e != nil {
				rows.Close()
				return e
			}
			existing[row.ID] = row
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		if len(existing) > store.MaxRecordingInventoryItems {
			return store.ErrRecordingState
		}
		for id, row := range existing {
			proof, ok := byID[id]
			if row.MemberID != n.ID || row.State == "deleted" && ok || row.State == "ready" && !ok {
				return store.ErrRecordingState
			}
			if ok && (row.UserID != proof.UserID || row.Bytes != proof.Bytes || row.Filename != proof.Filename || row.ContentType != proof.ContentType || row.SHA256 == nil || *row.SHA256 != proof.SHA256) {
				return store.ErrRecordingState
			}
		}
		for _, proof := range v.Recordings {
			var blocked int
			if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM karaoke_users WHERE user_id=? AND status IN ('deleting','deleted')", proof.UserID).Scan(&blocked); err != nil {
				return err
			}
			if blocked != 0 {
				return store.ErrRecordingState
			}
			if _, ok := existing[proof.ID]; ok {
				continue
			}
			if _, err = q.ExecContext(ctx, "INSERT INTO karaoke_recordings("+recordingColumns+") VALUES (?,?,?,?,?,?,?,'ready','','[]',?,?)", proof.ID, proof.UserID, n.ID, proof.Filename, proof.ContentType, proof.Bytes, proof.SHA256, proof.CreatedAt, now); err != nil {
				return err
			}
			if err = r.nodeAudit(ctx, q, "recording-owned-adopted", rel.ID, map[string]any{"recording_id": proof.ID, "user_id": proof.UserID, "size_bytes": proof.Bytes, "sha256": proof.SHA256, "master_id": v.MasterID}, a); err != nil {
				return err
			}
			adopted++
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return adopted, nil
}
