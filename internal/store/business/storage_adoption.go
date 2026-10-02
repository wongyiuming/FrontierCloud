package business

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"strings"
	"time"
)

const maxOwnedAdoptionObjects = 5000

func (r *Repository) adoptionObjects(ctx context.Context, q queryer) ([]store.MediaObject, error) {
	rows, err := q.QueryContext(ctx, "SELECT media_id,media_path,object_kind FROM media_objects WHERE object_kind IN ('audio','video') ORDER BY media_id LIMIT 5001"+r.lock())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []store.MediaObject{}
	for rows.Next() {
		var o store.MediaObject
		if err := rows.Scan(&o.ID, &o.Path, &o.Kind); err != nil {
			return nil, err
		}
		result = append(result, o)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(result) > maxOwnedAdoptionObjects {
		return nil, nodeConflict("owned adoption selection exceeds 5000 objects")
	}
	return result, nil
}

func (r *Repository) OwnedAdoptionObjects(ctx context.Context, relationship string) (objects []store.MediaObject, err error) {
	if !nodeIDPattern.MatchString(relationship) {
		return nil, store.ErrNodeState
	}
	err = r.write(ctx, func(q queryer) error {
		if _, err := r.ownedStorageNode(ctx, q, relationship); err != nil {
			return err
		}
		var err error
		objects, err = r.adoptionObjects(ctx, q)
		return err
	})
	if err != nil {
		return nil, err
	}
	return objects, nil
}

// The service owns an offline volume lease and checks exact registered files.
// This is accounting adoption, not permission to recreate bytes/IDs, replace
// relationships or refund preexisting quota. The write transaction rechecks
// the complete selected object set and current active upstream before commit.
func (r *Repository) AdoptOwnedStorage(ctx context.Context, relationship, expectedNode string, proofs []store.LocalMedia, a store.NodeAudit) (adopted int, err error) {
	if !nodeIDPattern.MatchString(relationship) || !nodeIDPattern.MatchString(expectedNode) || len(proofs) > maxOwnedAdoptionObjects {
		return 0, store.ErrNodeState
	}
	byID := map[string]store.LocalMedia{}
	var bytes int64
	for _, p := range proofs {
		digest := strings.Trim(p.ETag, `"`)
		if !nodeHashPattern.MatchString(p.ID) || !poolPath(p) || p.Bytes <= 0 || p.Bytes > 10*store.GiB || !nodeHashPattern.MatchString(digest) || p.ETag != `"`+digest+`"` || bytes > store.MaxStorageAllocation-p.Bytes {
			return 0, store.ErrNodeState
		}
		if _, exists := byID[p.ID]; exists {
			return 0, store.ErrNodeState
		}
		byID[p.ID] = p
		bytes += p.Bytes
	}
	err = r.write(ctx, func(q queryer) error {
		adopted = 0
		n, err := r.ownedStorageNode(ctx, q, relationship)
		if err != nil {
			return err
		}
		if n.ID != expectedNode {
			return store.ErrNodeState
		}
		for _, query := range []string{
			"SELECT COUNT(*) FROM media_delete_operations WHERE state<>'rename_done'",
			"SELECT COUNT(*) FROM cluster_upload_sessions WHERE state NOT IN ('complete','cancelled')",
			"SELECT COUNT(*) FROM global_media_objects",
		} {
			var count int64
			if err := q.QueryRowContext(ctx, query).Scan(&count); err != nil {
				return err
			}
			if count != 0 {
				return store.ErrBackupBusy
			}
		}
		member, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), n.ID))
		if err != nil {
			return err
		}
		if member.Kind != "Follower" || member.Reserved != 0 || member.Used < bytes || member.Used > member.Allocation {
			return nodeConflict("legacy storage counters do not cover the proven objects")
		}
		objects, err := r.adoptionObjects(ctx, q)
		if err != nil {
			return err
		}
		if len(objects) != len(proofs) {
			return nodeConflict("owned object set changed during adoption")
		}
		for _, object := range objects {
			proof, ok := byID[object.ID]
			if !ok || proof.Path != object.Path || proof.Kind != object.Kind {
				return nodeConflict("owned object binding changed during adoption")
			}
			rows, err := q.QueryContext(ctx, "SELECT "+uploadColumns+" FROM cluster_upload_sessions WHERE media_id=?"+r.lock(), object.ID)
			if err != nil {
				return err
			}
			var entries []store.UploadReservation
			for rows.Next() {
				v, err := scanUpload(rows)
				if err != nil {
					rows.Close()
					return err
				}
				entries = append(entries, v)
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
			if len(entries) > 1 {
				return store.ErrNodeState
			}
			if len(entries) == 1 {
				v := entries[0]
				if v.MemberID != n.ID || v.State != "complete" || v.Path != proof.Path || v.Kind != proof.Kind || v.ExpectedBytes != proof.Bytes {
					return nodeConflict("existing owned ledger differs from physical proof")
				}
				continue
			}
			identifier := sha256.Sum256([]byte("frontiercloud-owned-adoption-v1:" + n.ID + ":" + relationship + ":" + object.ID))
			id := hex.EncodeToString(identifier[:16])
			now := time.Now().Unix()
			if _, err := q.ExecContext(ctx, "INSERT INTO cluster_upload_sessions(upload_id,storage_member_id,media_id,media_path,path_locator,object_kind,expected_bytes,state,expires_at,created_at,updated_at) VALUES (?,?,?,?,NULL,?,?,'complete',0,?,?)", id, n.ID, proof.ID, proof.Path, proof.Kind, proof.Bytes, now, now); err != nil {
				return err
			}
			if err := r.nodeAudit(ctx, q, "storage-owned-adopted", relationship, map[string]any{"operation": id, "object_id": proof.ID, "path": proof.Path, "bytes": proof.Bytes, "etag": proof.ETag}, a); err != nil {
				return err
			}
			adopted++
		}
		// Catch orphan native ledger rows rather than silently double-accounting
		// or adopting only the subset that happens to have a current path.
		var count int64
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM cluster_upload_sessions WHERE storage_member_id=? AND state='complete'", n.ID).Scan(&count); err != nil {
			return err
		}
		if count != int64(len(proofs)) {
			return nodeConflict("orphan owned publication ledger")
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return adopted, nil
}
