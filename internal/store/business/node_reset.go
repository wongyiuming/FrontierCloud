package business

import (
	"context"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// Reset rotates only local identity. Encrypted revocation tombstones and cold
// history remain intact; neither old ownership nor a ready backup is reassigned.
func (r *Repository) ResetIdentity(ctx context.Context, confirmation, expectedRole string, next store.NodeIdentity, a store.NodeAudit) (result store.NodeIdentity, err error) {
	if !nodeIDPattern.MatchString(confirmation) || !nodeIDPattern.MatchString(next.ID) || next.ID == confirmation || next.Role != "Standalone" || next.Endpoint != "" || next.PrivateKey == "" {
		return result, nodeConflict("invalid reinitialization identity")
	}
	err = r.write(ctx, func(q queryer) error {
		old, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if old.ID != confirmation {
			return nodeConflict("请准确输入当前节点 ID 确认重新初始化")
		}
		if old.Role != expectedRole {
			return nodeConflict("node role changed during physical reset inspection")
		}
		if old.Role != "Standalone" && old.Role != "Master" && old.Role != "Follower" {
			return store.ErrNodeState
		}
		queries := []string{
			"SELECT COUNT(*) FROM global_media_objects",
			"SELECT COUNT(*) FROM media_delete_operations WHERE state<>'rename_done'",
			"SELECT COUNT(*) FROM cluster_upload_sessions WHERE state NOT IN ('complete','cancelled')",
			"SELECT COUNT(*) FROM cluster_storage_members WHERE reserved_bytes<>0",
			"SELECT COUNT(*) FROM cluster_worker_jobs WHERE state<>'complete'",
			"SELECT COUNT(*) FROM karaoke_users WHERE status='deleting'",
		}
		if old.Role != "Standalone" {
			queries = append(queries, "SELECT COUNT(*) FROM karaoke_recordings WHERE state<>'deleted'")
		}
		if old.Role == "Follower" {
			queries = append(queries, "SELECT COUNT(*) FROM media_objects WHERE object_kind IN ('audio','video') OR (object_kind='lyric' AND media_path<>'lyrics/default.lrc')")
		}
		for _, query := range queries {
			var count int64
			if err := q.QueryRowContext(ctx, query).Scan(&count); err != nil {
				return err
			}
			if count != 0 {
				return nodeConflict("节点仍有业务文件、预留或未完成事务，排空前禁止重新初始化")
			}
		}
		if _, err := q.ExecContext(ctx, "UPDATE node_relationships SET state='revoked',status='offline' WHERE state<>'revoked'"); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "UPDATE node_pair_packages SET state='revoked'"); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "UPDATE cluster_storage_members SET health='offline',writable=0"); err != nil {
			return err
		}
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			if _, err := q.ExecContext(ctx, "DELETE FROM "+table+" WHERE member_id=?", old.ID); err != nil {
				return err
			}
		}
		if _, err := q.ExecContext(ctx, "DELETE FROM cluster_upload_sessions WHERE storage_member_id=? AND state IN ('complete','cancelled')", old.ID); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "UPDATE node_identity SET node_id=?,`role`='Standalone',endpoint='',private_key=? WHERE singleton=1", next.ID, next.PrivateKey); err != nil {
			return err
		}
		if err := r.nodeAudit(ctx, q, "reinitialize", "", map[string]any{"old_node_id": old.ID, "new_node_id": next.ID}, a); err != nil {
			return err
		}
		next.CreatedAt = old.CreatedAt
		result = next
		return nil
	})
	if err != nil {
		return store.NodeIdentity{}, err
	}
	return result, nil
}
