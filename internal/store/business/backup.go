package business

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

const backupColumns = "master_id,generation,checksum,size_bytes,chunk_count,state,created_at,updated_at"

func scanBackup(row interface{ Scan(...any) error }) (v store.BackupManifest, err error) {
	err = row.Scan(&v.MasterID, &v.Generation, &v.Checksum, &v.Bytes, &v.Chunks, &v.State, &v.CreatedAt, &v.UpdatedAt)
	return
}

func (r *Repository) backupUpstream(ctx context.Context, q queryer, relationship string) (store.NodeIdentity, store.Relationship, error) {
	if !nodeIDPattern.MatchString(relationship) {
		return store.NodeIdentity{}, store.Relationship{}, store.ErrNodeState
	}
	node, err := readNode(ctx, q, r.lock())
	if err != nil {
		return node, store.Relationship{}, err
	}
	if node.Role != "Follower" {
		return node, store.Relationship{}, store.ErrNodeState
	}
	rel, err := scanRelation(q.QueryRowContext(ctx, "SELECT "+relationColumns+" FROM node_relationships WHERE relationship_id=?"+r.lock(), relationship))
	if errors.Is(err, sql.ErrNoRows) {
		return node, rel, store.ErrNodeState
	}
	if err != nil {
		return node, rel, err
	}
	if rel.State != "active" || rel.Direction != "upstream" || rel.Protocol != 2 || !nodeIDPattern.MatchString(rel.PeerID) {
		return node, rel, store.ErrNodeState
	}
	return node, rel, nil
}

func (r *Repository) BeginBackup(ctx context.Context, relationship string, generation int64, a store.NodeAudit) error {
	if generation <= 0 {
		return store.ErrBackupState
	}
	return r.write(ctx, func(q queryer) error {
		node, rel, err := r.backupUpstream(ctx, q, relationship)
		if err != nil {
			return err
		}
		old, err := scanBackup(q.QueryRowContext(ctx, "SELECT "+backupColumns+" FROM cluster_business_backups WHERE master_id=? AND generation=?"+r.lock(), rel.PeerID, generation))
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		// A retry must never erase an already verified recovery generation.
		if err == nil && old.State == "ready" {
			return store.ErrBackupState
		}
		now := time.Now().Unix()
		if _, err = q.ExecContext(ctx, "DELETE FROM cluster_business_backup_chunks WHERE master_id=? AND generation=?", rel.PeerID, generation); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "DELETE FROM cluster_business_backups WHERE master_id=? AND generation=?", rel.PeerID, generation); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "INSERT INTO cluster_business_backups("+backupColumns+") VALUES (?,?,'',0,0,'receiving',?,?)", rel.PeerID, generation, now, now); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_backup_members SET state='receiving',updated_at=? WHERE member_id=? AND enabled=1", now, node.ID); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "backup-begin", relationship, map[string]any{"generation": generation, "master_id": rel.PeerID}, a)
	})
}

func (r *Repository) AppendBackup(ctx context.Context, relationship string, generation int64, index int, chunk []byte) error {
	if generation <= 0 || index < 0 || index > store.MaxBackupChunkIndex || len(chunk) > store.MaxBackupChunk {
		return store.ErrBackupState
	}
	if chunk == nil {
		chunk = []byte{}
	} // Empty protocol chunks are BLOBs, not SQL NULL.
	return r.write(ctx, func(q queryer) error {
		_, rel, err := r.backupUpstream(ctx, q, relationship)
		if err != nil {
			return err
		}
		manifest, err := scanBackup(q.QueryRowContext(ctx, "SELECT "+backupColumns+" FROM cluster_business_backups WHERE master_id=? AND generation=?"+r.lock(), rel.PeerID, generation))
		if errors.Is(err, sql.ErrNoRows) {
			return store.ErrBackupState
		}
		if err != nil {
			return err
		}
		if manifest.State != "receiving" {
			return store.ErrBackupState
		}
		var old []byte
		err = q.QueryRowContext(ctx, "SELECT payload FROM cluster_business_backup_chunks WHERE master_id=? AND generation=? AND chunk_index=?", rel.PeerID, generation, index).Scan(&old)
		if err == nil {
			// Lost HTTP acknowledgements may replay identical bytes, but never
			// replace a received chunk with different content.
			if bytes.Equal(old, chunk) {
				return nil
			}
			return store.ErrBackupState
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		_, err = q.ExecContext(ctx, "INSERT INTO cluster_business_backup_chunks(master_id,generation,chunk_index,payload,created_at) VALUES (?,?,?,?,?)", rel.PeerID, generation, index, chunk, time.Now().Unix())
		return err
	})
}

func (r *Repository) CommitBackup(ctx context.Context, relationship string, generation int64, checksum string, a store.NodeAudit) (result store.BackupManifest, err error) {
	if generation <= 0 || !nodeHashPattern.MatchString(checksum) {
		return result, store.ErrBackupState
	}
	err = r.write(ctx, func(q queryer) error {
		node, rel, err := r.backupUpstream(ctx, q, relationship)
		if err != nil {
			return err
		}
		manifest, err := scanBackup(q.QueryRowContext(ctx, "SELECT "+backupColumns+" FROM cluster_business_backups WHERE master_id=? AND generation=?"+r.lock(), rel.PeerID, generation))
		if errors.Is(err, sql.ErrNoRows) {
			return store.ErrBackupState
		}
		if err != nil {
			return err
		}
		if manifest.State == "ready" && manifest.Checksum == checksum {
			result = manifest
			return nil
		}
		if manifest.State != "receiving" {
			return store.ErrBackupState
		}
		// Stream rows in the transaction: only one <=192 KiB chunk is held,
		// including on MySQL. Do not materialize the full artifact in memory.
		rows, err := q.QueryContext(ctx, "SELECT chunk_index,payload FROM cluster_business_backup_chunks WHERE master_id=? AND generation=? ORDER BY chunk_index", rel.PeerID, generation)
		if err != nil {
			return err
		}
		digest, count, size := sha256.New(), 0, int64(0)
		for rows.Next() {
			var index int
			var chunk []byte
			if err := rows.Scan(&index, &chunk); err != nil {
				rows.Close()
				return err
			}
			if index != count || index > store.MaxBackupChunkIndex || len(chunk) > store.MaxBackupChunk {
				rows.Close()
				return store.ErrBackupState
			}
			digest.Write(chunk)
			count++
			size += int64(len(chunk))
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		if count == 0 || hex.EncodeToString(digest.Sum(nil)) != checksum {
			return store.ErrBackupState
		}
		now := time.Now().Unix()
		if _, err = q.ExecContext(ctx, "UPDATE cluster_business_backups SET checksum=?,size_bytes=?,chunk_count=?,state='ready',updated_at=? WHERE master_id=? AND generation=?", checksum, size, count, now, rel.PeerID, generation); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_backup_members SET generation=?,last_success=?,lag_seconds=0,checksum=?,state='ready',updated_at=? WHERE member_id=? AND generation<=?", generation, now, checksum, now, node.ID, generation); err != nil {
			return err
		}
		// Retain exactly the newest two verified generations for this Master.
		for {
			var old int64
			err := q.QueryRowContext(ctx, "SELECT generation FROM cluster_business_backups WHERE master_id=? AND state='ready' ORDER BY generation DESC LIMIT 1 OFFSET 2", rel.PeerID).Scan(&old)
			if errors.Is(err, sql.ErrNoRows) {
				break
			}
			if err != nil {
				return err
			}
			if _, err := q.ExecContext(ctx, "DELETE FROM cluster_business_backup_chunks WHERE master_id=? AND generation=?", rel.PeerID, old); err != nil {
				return err
			}
			if _, err := q.ExecContext(ctx, "DELETE FROM cluster_business_backups WHERE master_id=? AND generation=?", rel.PeerID, old); err != nil {
				return err
			}
		}
		if err := r.nodeAudit(ctx, q, "backup-ready", relationship, map[string]any{"generation": generation, "checksum": checksum, "bytes": size, "chunks": count}, a); err != nil {
			return err
		}
		manifest.Checksum, manifest.Bytes, manifest.Chunks, manifest.State, manifest.UpdatedAt = checksum, size, count, "ready", now
		result = manifest
		return nil
	})
	return
}

func (r *Repository) AbortBackups(ctx context.Context, relationship string, generation int64, a store.NodeAudit) (result store.BackupAbort, err error) {
	if generation <= 0 {
		return result, store.ErrBackupState
	}
	err = r.write(ctx, func(q queryer) error {
		node, rel, err := r.backupUpstream(ctx, q, relationship)
		if err != nil {
			return err
		}
		var receiving int
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM cluster_business_backups WHERE master_id=? AND state='receiving'", rel.PeerID).Scan(&receiving); err != nil {
			return err
		}
		result = store.BackupAbort{Status: "clean", Generation: generation, AbortedGenerations: receiving}
		if receiving == 0 {
			return nil
		}
		now := time.Now().Unix()
		if _, err := q.ExecContext(ctx, "DELETE FROM cluster_business_backup_chunks WHERE master_id=? AND generation IN (SELECT generation FROM cluster_business_backups WHERE master_id=? AND state='receiving')", rel.PeerID, rel.PeerID); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "UPDATE cluster_business_backups SET state='failed',size_bytes=0,chunk_count=0,updated_at=? WHERE master_id=? AND state='receiving'", now, rel.PeerID); err != nil {
			return err
		}
		if _, err := q.ExecContext(ctx, "UPDATE cluster_backup_members SET state='failed',updated_at=? WHERE member_id=? AND enabled=1", now, node.ID); err != nil {
			return err
		}
		result.Status = "failed"
		return r.nodeAudit(ctx, q, "backup-aborted", relationship, map[string]any{"generation": generation, "aborted_generations": receiving}, a)
	})
	return
}
