package business

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"path"
	"sort"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

const globalColumns = "g.media_id,g.storage_member_id,g.object_id,g.media_path,g.object_kind,g.size_bytes,g.etag,g.state,g.created_at,g.updated_at,s.relationship_id,s.health,s.transport,COALESCE(p.play_score,0),COALESCE(p.preference,0),EXISTS(SELECT 1 FROM media_lyric_links l WHERE l.media_id=g.media_id)"
const globalTables = " FROM global_media_objects g JOIN cluster_storage_members s ON s.member_id=g.storage_member_id LEFT JOIN media_playback_stats p ON p.media_id=g.media_id"

func scanGlobal(row rowScanner) (v store.GlobalMedia, err error) {
	err = row.Scan(&v.ID, &v.MemberID, &v.ObjectID, &v.Path, &v.Kind, &v.Bytes, &v.ETag, &v.State, &v.CreatedAt, &v.UpdatedAt, &v.RelationshipID, &v.Health, &v.Transport, &v.PlayScore, &v.Preference, &v.HasLyrics)
	return
}
func (r *Repository) Resources(ctx context.Context, scope string, exact bool) ([]store.GlobalMedia, error) {
	node, err := r.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if node.Role != "Master" {
		return nil, nodeConflict("only Master owns the global catalog")
	}
	query := "SELECT " + globalColumns + globalTables + " WHERE g.state='active'"
	args := []any{}
	if scope != "" {
		if exact {
			query += " AND g.path_locator=? AND g.media_path=?"
			args = append(args, locator(scope), scope)
		} else {
			where, values := r.pathScope("g.media_path", strings.TrimSuffix(scope, "/"), true)
			query += " AND " + where
			args = append(args, values...)
		}
	}
	query += " ORDER BY g.media_path,g.media_id"
	rows, err := r.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []store.GlobalMedia{}
	for rows.Next() {
		v, err := scanGlobal(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, v)
	}
	return result, rows.Err()
}
func (r *Repository) Resource(ctx context.Context, id string) (store.GlobalMedia, error) {
	if !nodeHashPattern.MatchString(id) {
		return store.GlobalMedia{}, nodeConflict("invalid global media identity")
	}
	node, err := r.ReadIdentity(ctx)
	if err != nil {
		return store.GlobalMedia{}, err
	}
	if node.Role != "Master" {
		return store.GlobalMedia{}, nodeConflict("only Master owns the global catalog")
	}
	v, err := scanGlobal(r.db.QueryRowContext(ctx, "SELECT "+globalColumns+globalTables+" WHERE g.media_id=? AND g.state='active'", id))
	if errors.Is(err, sql.ErrNoRows) {
		err = nodeConflict("global media object not found")
	}
	return v, err
}

const uploadColumns = "upload_id,storage_member_id,media_id,media_path,object_kind,expected_bytes,state,expires_at,created_at,updated_at"

func scanUpload(row rowScanner) (v store.UploadReservation, err error) {
	err = row.Scan(&v.ID, &v.MemberID, &v.MediaID, &v.Path, &v.Kind, &v.ExpectedBytes, &v.State, &v.ExpiresAt, &v.CreatedAt, &v.UpdatedAt)
	return
}
func (r *Repository) Upload(ctx context.Context, id string) (v store.UploadReservation, err error) {
	if !nodeIDPattern.MatchString(id) {
		return v, nodeConflict("invalid upload session")
	}
	v, err = scanUpload(r.db.QueryRowContext(ctx, "SELECT "+uploadColumns+" FROM cluster_upload_sessions WHERE upload_id=?", id))
	if errors.Is(err, sql.ErrNoRows) {
		err = nodeConflict("upload session not found")
	}
	if err != nil {
		return
	}
	v.Member, err = scanMember(r.db.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?", v.MemberID))
	return
}
func memberSite(v store.StorageMember) string {
	if v.Kind == "MasterLocal" {
		return "primary"
	}
	if v.Transport == "Direct" {
		return "direct"
	}
	if v.Transport == "Relay" {
		return "relay"
	}
	return ""
}
func capacity(v store.StorageMember) int64 {
	if v.Enabled == 0 || v.Writable == 0 || v.Health != "online" {
		return 0
	}
	// Reserved transfers are not yet reflected in physical free space. Account
	// for them here as well, so a large logical allocation cannot oversubscribe
	// a small physical volume through concurrent uploads.
	return min(max(0, v.Allocation-v.Used-v.Reserved), max(0, v.PhysicalFree-store.PhysicalReserve-v.Reserved))
}
func (r *Repository) ReserveUpload(ctx context.Context, name, site string, size, localPhysicalFree int64, a store.AdminAudit) (result store.UploadReservation, err error) {
	kind := "audio"
	if strings.HasPrefix(name, "vido/") {
		kind = "video"
	}
	if !poolPath(store.LocalMedia{MediaObject: store.MediaObject{Path: name, Kind: kind}, Bytes: size}) || size <= 0 || size > 10*store.GiB || (site != "primary" && site != "direct" && site != "relay") || localPhysicalFree < 0 {
		return result, nodeConflict("invalid upload reservation")
	}
	id, err := randomID()
	if err != nil {
		return result, err
	}
	id = id[:32]
	mediaID := locator(id + ":" + name)
	now := time.Now().Unix()
	err = r.write(ctx, func(q queryer) error {
		node, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if node.Role != "Master" {
			return nodeConflict("Storage Pool uploads require a Master")
		}
		var count int
		if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM global_media_objects WHERE path_locator=?", locator(name)).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return nodeConflict("global media path already exists")
		}
		if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM cluster_upload_sessions WHERE path_locator=?", locator(name)).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return nodeConflict("global media path already has a durable upload reservation")
		}
		// Both active placements and reservations participate in the category
		// layout fence. Expiry alone never proves that physical bytes are gone.
		category := strings.Join(strings.Split(name, "/")[:2], "/")
		where, args := r.pathScope("media_path", category, true)
		query := "SELECT media_path,storage_member_id FROM global_media_objects WHERE state IN ('active','pending_delete') AND " + where + " UNION ALL SELECT media_path,storage_member_id FROM cluster_upload_sessions WHERE state='reserved' AND " + where
		rows, err := q.QueryContext(ctx, query, append(args, args...)...)
		if err != nil {
			return err
		}
		affinity := ""
		folder := path.Dir(name)
		depth := strings.Count(name, "/")
		for rows.Next() {
			var existing, owner string
			if err = rows.Scan(&existing, &owner); err != nil {
				rows.Close()
				return err
			}
			if strings.Count(existing, "/") != depth {
				rows.Close()
				return nodeConflict("category cannot mix direct media and nested media")
			}
			if path.Dir(existing) == folder {
				if affinity != "" && affinity != owner {
					rows.Close()
					return nodeConflict("historical media folder is spread across multiple storage members")
				}
				affinity = owner
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		rows, err = q.QueryContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members ORDER BY member_id"+r.lock())
		if err != nil {
			return err
		}
		members := []store.StorageMember{}
		for rows.Next() {
			v, err := scanMember(rows)
			if err != nil {
				rows.Close()
				return err
			}
			members = append(members, v)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		candidates := []store.StorageMember{}
		for _, v := range members {
			if v.ID == node.ID && v.Kind == "MasterLocal" {
				v.PhysicalFree = localPhysicalFree
			}
			if v.RelationshipID != nil {
				var state, status string
				var last int64
				if err = q.QueryRowContext(ctx, "SELECT state,status,last_heartbeat FROM node_relationships WHERE relationship_id=?"+r.lock(), *v.RelationshipID).Scan(&state, &status, &last); err != nil {
					return err
				}
				if state != "active" || status == "offline" || now-last >= 120 {
					v.Writable = 0
				}
			}
			if affinity != "" && v.ID != affinity {
				continue
			}
			if memberSite(v) != site {
				if affinity != "" {
					return nodeConflict("media folder belongs to a different site type")
				}
				continue
			}
			v.Available = capacity(v)
			if v.Available >= size {
				candidates = append(candidates, v)
			}
		}
		if len(candidates) == 0 {
			return nodeConflict("no online writable member has enough capacity for this media folder")
		}
		sort.Slice(candidates, func(i, j int) bool {
			x, y := candidates[i], candidates[j]
			px := float64(x.Used+x.Reserved) / float64(max(int64(1), x.Allocation))
			py := float64(y.Used+y.Reserved) / float64(max(int64(1), y.Allocation))
			if px != py {
				return px < py
			}
			if x.Available != y.Available {
				return x.Available > y.Available
			}
			return x.ID < y.ID
		})
		member := candidates[0]
		if _, err = q.ExecContext(ctx, "UPDATE cluster_storage_members SET reserved_bytes=reserved_bytes+?,physical_free_bytes=?,updated_at=? WHERE member_id=?", size, member.PhysicalFree, now, member.ID); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "INSERT INTO cluster_upload_sessions(upload_id,storage_member_id,media_id,media_path,path_locator,object_kind,expected_bytes,state,expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'reserved',?,?,?)", id, member.ID, mediaID, name, locator(name), kind, size, now+1800, now, now); err != nil {
			return err
		}
		result = store.UploadReservation{ID: id, MemberID: member.ID, MediaID: mediaID, Path: name, Kind: kind, ExpectedBytes: size, State: "reserved", ExpiresAt: now + 1800, CreatedAt: now, UpdatedAt: now, Member: member}
		detail, _ := json.Marshal(map[string]any{"upload_id": id, "media_id": mediaID, "member_id": member.ID, "site_type": site, "expected_bytes": size})
		a.Action = "upload-reserved"
		a.Detail = string(detail)
		a.Result = "success"
		a.TargetCount = 1
		return appendMutationAudit(ctx, q, a, []string{name})
	})
	return
}
func (r *Repository) FinalizeUpload(ctx context.Context, id, objectID string, actualSize int64, etag string, a store.AdminAudit) (result store.GlobalMedia, err error) {
	if !nodeIDPattern.MatchString(id) || !nodeHashPattern.MatchString(objectID) || actualSize <= 0 || len(etag) == 0 || len(etag) > 128 || strings.ContainsAny(etag, "\r\n\x00") {
		return result, nodeConflict("invalid upload completion")
	}
	err = r.write(ctx, func(q queryer) error {
		node, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if node.Role != "Master" {
			return nodeConflict("only Master finalizes business uploads")
		}
		v, err := scanUpload(q.QueryRowContext(ctx, "SELECT "+uploadColumns+" FROM cluster_upload_sessions WHERE upload_id=?"+r.lock(), id))
		if errors.Is(err, sql.ErrNoRows) {
			return nodeConflict("upload reservation not found")
		}
		if err != nil {
			return err
		}
		if v.MediaID != objectID || v.ExpectedBytes != actualSize {
			return nodeConflict("uploaded object identity or size does not match its reservation")
		}
		if v.State == "complete" {
			result, err = scanGlobal(q.QueryRowContext(ctx, "SELECT "+globalColumns+globalTables+" WHERE g.media_id=?", v.MediaID))
			if err != nil {
				return err
			}
			if result.ObjectID != objectID || result.MemberID != v.MemberID || result.Path != v.Path || result.Bytes != actualSize || result.ETag != etag || result.State != "active" {
				return nodeConflict("completed upload cannot be replaced")
			}
			return nil
		}
		if v.State != "reserved" || v.ExpiresAt <= time.Now().Unix() {
			return nodeConflict("upload reservation missing or expired")
		}
		member, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), v.MemberID))
		if err != nil {
			return err
		}
		if member.Reserved < v.ExpectedBytes || member.Allocation-member.Used < actualSize {
			return nodeConflict("upload capacity reservation is inconsistent")
		}
		now := time.Now().Unix()
		if _, err = q.ExecContext(ctx, "INSERT INTO global_media_objects(media_id,storage_member_id,object_id,media_path,path_locator,object_kind,size_bytes,etag,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'active',?,?)", v.MediaID, v.MemberID, objectID, v.Path, locator(v.Path), v.Kind, actualSize, etag, now, now); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_storage_members SET reserved_bytes=reserved_bytes-?,used_bytes=used_bytes+?,updated_at=? WHERE member_id=?", v.ExpectedBytes, actualSize, now, v.MemberID); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_upload_sessions SET state='complete',path_locator=NULL,updated_at=? WHERE upload_id=?", now, id); err != nil {
			return err
		}
		result = store.GlobalMedia{ID: v.MediaID, MemberID: v.MemberID, ObjectID: objectID, Path: v.Path, Kind: v.Kind, Bytes: actualSize, ETag: etag, State: "active", CreatedAt: now, UpdatedAt: now, RelationshipID: member.RelationshipID, Health: member.Health, Transport: member.Transport}
		detail, _ := json.Marshal(map[string]any{"upload_id": id, "media_id": v.MediaID, "member_id": v.MemberID, "size_bytes": actualSize})
		a.Action = "upload-finalized"
		a.Detail = string(detail)
		a.Result = "success"
		a.TargetCount = 1
		return appendMutationAudit(ctx, q, a, []string{v.Path})
	})
	return
}

// ReleaseCleanedUpload requires the service to establish that its physical
// placement was removed or never published. Never call it merely on timeout.
func (r *Repository) ReleaseCleanedUpload(ctx context.Context, id string, a store.AdminAudit) error {
	if !nodeIDPattern.MatchString(id) {
		return nodeConflict("invalid upload session")
	}
	return r.write(ctx, func(q queryer) error {
		node, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if node.Role != "Master" {
			return nodeConflict("only Master releases a business reservation")
		}
		v, err := scanUpload(q.QueryRowContext(ctx, "SELECT "+uploadColumns+" FROM cluster_upload_sessions WHERE upload_id=?"+r.lock(), id))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if v.State == "complete" {
			return nodeConflict("completed placement must use the deletion workflow")
		}
		if v.State != "reserved" {
			return nodeConflict("upload reservation state conflict")
		}
		var count int
		if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM global_media_objects WHERE media_id=?", v.MediaID).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return nodeConflict("reservation still has a committed media object")
		}
		member, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), v.MemberID))
		if err != nil {
			return err
		}
		if member.Reserved < v.ExpectedBytes {
			return nodeConflict("upload capacity reservation is inconsistent")
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_storage_members SET reserved_bytes=reserved_bytes-?,updated_at=? WHERE member_id=?", v.ExpectedBytes, time.Now().Unix(), v.MemberID); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "DELETE FROM cluster_upload_sessions WHERE upload_id=?", id); err != nil {
			return err
		}
		a.Action = "upload-reservation-cleaned"
		a.Result = "success"
		a.TargetCount = 1
		return appendMutationAudit(ctx, q, a, []string{v.Path})
	})
}
func (r *Repository) ExpiredUploads(ctx context.Context, limit int) ([]store.UploadReservation, error) {
	if limit < 1 || limit > 500 {
		limit = 50
	}
	rows, err := r.db.QueryContext(ctx, "SELECT "+uploadColumns+" FROM cluster_upload_sessions WHERE state='reserved' AND expires_at<=? ORDER BY created_at,upload_id LIMIT ?", time.Now().Unix(), limit)
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
