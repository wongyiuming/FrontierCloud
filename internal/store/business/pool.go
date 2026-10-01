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

func poolPath(v store.LocalMedia) bool {
	parts := strings.Split(v.Path, "/")
	if len(parts) < 3 || len(parts) > 4 || path.Clean(v.Path) != v.Path || strings.ContainsAny(v.Path, "\\\x00") || utf8.RuneCountInString(v.Path) > 1024 || v.Bytes < 0 || len(v.ETag) > 128 {
		return false
	}
	for _, part := range parts {
		if part == "" || strings.HasPrefix(part, ".") {
			return false
		}
	}
	ext := strings.ToLower(path.Ext(v.Path))
	return (v.Kind == "audio" && parts[0] == "music" && (ext == ".mp3" || ext == ".m4a" || ext == ".flac" || ext == ".wav")) || (v.Kind == "video" && parts[0] == "vido" && (ext == ".mp4" || ext == ".webm" || ext == ".mkv"))
}
func validConfiguration(c store.ResourceConfiguration) bool {
	return c.Storage.Allocation >= 0 && c.Storage.Allocation <= store.MaxStorageAllocation && (!c.Storage.Enabled || c.Storage.Allocation >= store.GiB) && c.Compute.Slots >= 0 && c.Compute.Slots <= 256
}

const memberColumns = "member_id,relationship_id,member_kind,transport,storage_enabled,allocated_bytes,used_bytes,reserved_bytes,physical_free_bytes,health,writable,updated_at"

func scanMember(row rowScanner) (v store.StorageMember, err error) {
	err = row.Scan(&v.ID, &v.RelationshipID, &v.Kind, &v.Transport, &v.Enabled, &v.Allocation, &v.Used, &v.Reserved, &v.PhysicalFree, &v.Health, &v.Writable, &v.UpdatedAt)
	return
}
func (r *Repository) upsertMember(ctx context.Context, q queryer, v store.StorageMember, columns []string) error {
	query := "INSERT INTO cluster_storage_members(" + memberColumns + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
	if r.backend == "sqlite" {
		query += " ON CONFLICT(member_id) DO UPDATE SET "
		for i, column := range columns {
			if i > 0 {
				query += ","
			}
			query += column + "=excluded." + column
		}
	} else {
		query += " ON DUPLICATE KEY UPDATE "
		for i, column := range columns {
			if i > 0 {
				query += ","
			}
			query += column + "=VALUES(" + column + ")"
		}
	}
	_, err := q.ExecContext(ctx, query, v.ID, v.RelationshipID, v.Kind, v.Transport, v.Enabled, v.Allocation, v.Used, v.Reserved, v.PhysicalFree, v.Health, v.Writable, v.UpdatedAt)
	return err
}
func (r *Repository) initializeAuxMembers(ctx context.Context, q queryer, id string, now int64) error {
	if _, err := q.ExecContext(ctx, r.ignoreInsert()+" INTO cluster_compute_members(member_id,enabled,worker_slots,available_slots,cpu_percent,memory_available_bytes,capabilities,updated_at) VALUES (?,0,0,0,0,0,'[]',?)", id, now); err != nil {
		return err
	}
	_, err := q.ExecContext(ctx, r.ignoreInsert()+" INTO cluster_backup_members(member_id,enabled,generation,last_success,lag_seconds,checksum,state,updated_at) VALUES (?,0,0,0,0,'','disabled',?)", id, now)
	return err
}
func peerInteger(summary map[string]any, name string) int64 {
	var result int64
	switch value := summary[name].(type) {
	case json.Number:
		result, _ = value.Int64()
	case int64:
		result = value
	case int:
		result = int64(value)
	}
	return max(0, result)
}
func (r *Repository) registerFollower(ctx context.Context, q queryer, v store.Relationship) error {
	current, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), v.PeerID))
	if errors.Is(err, sql.ErrNoRows) {
		current = store.StorageMember{ID: v.PeerID}
	} else if err != nil {
		return err
	}
	var mediaUsed, recordingUsed int64
	if err = q.QueryRowContext(ctx, "SELECT COALESCE(SUM(size_bytes),0) FROM global_media_objects WHERE storage_member_id=? AND state IN ('active','pending_delete')", v.PeerID).Scan(&mediaUsed); err != nil {
		return err
	}
	if err = q.QueryRowContext(ctx, "SELECT COALESCE(SUM(size_bytes),0) FROM karaoke_recordings WHERE storage_member_id=? AND state='ready'", v.PeerID).Scan(&recordingUsed); err != nil {
		return err
	}
	current.Used = max(current.Used, mediaUsed+recordingUsed)
	current.RelationshipID = &v.ID
	current.Kind = "Follower"
	current.Transport = v.Mode
	current.Health = "offline"
	current.Writable = 0
	if v.State == "active" && v.Status == "online" {
		current.Health = "online"
		if current.Enabled != 0 {
			current.Writable = 1
		}
	}
	storage, _ := v.Summary["storage"].(map[string]any)
	current.PhysicalFree = peerInteger(storage, "physical_free_bytes")
	if current.PhysicalFree == 0 {
		current.PhysicalFree = peerInteger(v.Summary, "storage_free")
	}
	current.UpdatedAt = time.Now().Unix()
	if err = r.upsertMember(ctx, q, current, []string{"relationship_id", "member_kind", "transport", "used_bytes", "physical_free_bytes", "health", "writable", "updated_at"}); err != nil {
		return err
	}
	if err = r.initializeAuxMembers(ctx, q, current.ID, current.UpdatedAt); err != nil {
		return err
	}
	_, err = q.ExecContext(ctx, "UPDATE cluster_compute_members SET enabled=0,worker_slots=0,available_slots=0,updated_at=? WHERE member_id=?", current.UpdatedAt, current.ID)
	return err
}
func (r *Repository) adoptLocal(ctx context.Context, q queryer, node store.NodeIdentity, physicalUsed, physicalFree int64, items []store.LocalMedia, allocation int64) error {
	now := time.Now().Unix()
	seen := map[string]bool{}
	for _, item := range items {
		if !poolPath(item) || seen[item.Path] {
			return nodeConflict("invalid local media adoption manifest")
		}
		seen[item.Path] = true
		id, err := r.ensureWithLock(ctx, q, item.MediaObject, r.lock())
		if err != nil {
			return err
		}
		if item.ID != "" && id != item.ID {
			return nodeConflict("local media identity mismatch")
		}
		var existingID, owner, object, state string
		err = q.QueryRowContext(ctx, "SELECT media_id,storage_member_id,object_id,state FROM global_media_objects WHERE path_locator=?"+r.lock(), locator(item.Path)).Scan(&existingID, &owner, &object, &state)
		if err == nil {
			if existingID != id || owner != node.ID || object != id || state != "active" {
				return nodeConflict("global media placement conflict")
			}
			if _, err = q.ExecContext(ctx, "UPDATE global_media_objects SET size_bytes=?,etag=?,updated_at=? WHERE media_id=?", item.Bytes, item.ETag, item.UpdatedAt, id); err != nil {
				return err
			}
		} else if errors.Is(err, sql.ErrNoRows) {
			if _, err = q.ExecContext(ctx, "INSERT INTO global_media_objects(media_id,storage_member_id,object_id,media_path,path_locator,object_kind,size_bytes,etag,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'active',?,?)", id, node.ID, id, item.Path, locator(item.Path), item.Kind, item.Bytes, item.ETag, item.CreatedAt, item.UpdatedAt); err != nil {
				return err
			}
		} else {
			return err
		}
	}
	var mediaUsed, recordingUsed int64
	if err := q.QueryRowContext(ctx, "SELECT COALESCE(SUM(size_bytes),0) FROM global_media_objects WHERE storage_member_id=? AND state IN ('active','pending_delete')", node.ID).Scan(&mediaUsed); err != nil {
		return err
	}
	if err := q.QueryRowContext(ctx, "SELECT COALESCE(SUM(size_bytes),0) FROM karaoke_recordings WHERE storage_member_id=? AND state='ready'", node.ID).Scan(&recordingUsed); err != nil {
		return err
	}
	used := max(mediaUsed, physicalUsed) + recordingUsed
	current, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), node.ID))
	if errors.Is(err, sql.ErrNoRows) {
		current = store.StorageMember{ID: node.ID}
	} else if err != nil {
		return err
	}
	if allocation == 0 {
		allocation = max(store.GiB, current.Allocation, used+current.Reserved)
	}
	if allocation < used || allocation-used < current.Reserved {
		return nodeConflict("Master Local allocation cannot be lower than used and reserved capacity")
	}
	current.Kind = "MasterLocal"
	current.Transport = "Local"
	current.Enabled = 1
	current.Allocation = allocation
	current.Used = used
	current.PhysicalFree = max(0, physicalFree)
	current.Health = "online"
	current.Writable = 1
	current.UpdatedAt = now
	if err = r.upsertMember(ctx, q, current, []string{"member_kind", "transport", "storage_enabled", "allocated_bytes", "used_bytes", "physical_free_bytes", "health", "writable", "updated_at"}); err != nil {
		return err
	}
	if err = r.initializeAuxMembers(ctx, q, node.ID, now); err != nil {
		return err
	}
	// Only the Master executes its local fallback jobs. The Follower Compute
	// Worker feature is retired and must never be enabled by legacy settings.
	_, err = q.ExecContext(ctx, "UPDATE cluster_compute_members SET enabled=1,worker_slots=1,available_slots=1,capabilities='[\"fallback\"]',updated_at=? WHERE member_id=?", now, node.ID)
	return err
}
func (r *Repository) PromoteIdentity(ctx context.Context, p store.NodePromotion, a store.NodeAudit) (result store.NodeIdentity, err error) {
	if (p.Role != "Master" && p.Role != "Follower") || p.Endpoint == "" || len(p.Endpoint) > 512 || p.PhysicalUsed < 0 || p.PhysicalFree < 0 {
		return result, nodeConflict("invalid promotion")
	}
	if p.Role == "Master" && (p.Allocation < store.GiB || p.Allocation > store.MaxStorageAllocation) {
		return result, nodeConflict("Master requires a Local Storage Allocation of at least 1 GiB")
	}
	err = r.write(ctx, func(q queryer) error {
		var err error
		result, err = readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if result.Role != "Standalone" {
			return nodeConflict("node role is fixed until explicit reinitialization")
		}
		if p.Role == "Follower" {
			if len(p.Media) != 0 || p.PhysicalUsed != 0 {
				return nodeConflict("Follower must have no existing business files")
			}
			var users, recordings, objects int
			if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM karaoke_users").Scan(&users); err != nil {
				return err
			}
			if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM karaoke_recordings").Scan(&recordings); err != nil {
				return err
			}
			if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM media_objects WHERE object_kind IN ('audio','video') OR (object_kind='lyric' AND media_path<>'lyrics/default.lrc')").Scan(&objects); err != nil {
				return err
			}
			if users+recordings+objects > 0 {
				return nodeConflict("Follower still has Standalone business data")
			}
		} else if err = r.adoptLocal(ctx, q, result, p.PhysicalUsed, p.PhysicalFree, p.Media, p.Allocation); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", p.Role, p.Endpoint); err != nil {
			return err
		}
		if err = r.nodeAudit(ctx, q, "promote", "", map[string]any{"role": p.Role, "endpoint": p.Endpoint, "local_capacity_bytes": p.Allocation}, a); err != nil {
			return err
		}
		result.Role = p.Role
		result.Endpoint = p.Endpoint
		return nil
	})
	return
}
func (r *Repository) AdoptMasterLocal(ctx context.Context, physicalUsed, physicalFree int64, items []store.LocalMedia) error {
	if physicalUsed < 0 || physicalFree < 0 {
		return nodeConflict("invalid physical capacity")
	}
	return r.write(ctx, func(q queryer) error {
		node, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if node.Role != "Master" {
			return nodeConflict("only Master adopts local media")
		}
		return r.adoptLocal(ctx, q, node, physicalUsed, physicalFree, items, 0)
	})
}
func (r *Repository) ConfigureMember(ctx context.Context, id string, c store.ResourceConfiguration, a store.NodeAudit) error {
	if !nodeIDPattern.MatchString(id) || !validConfiguration(c) {
		return nodeConflict("invalid resource configuration")
	}
	return r.write(ctx, func(q queryer) error {
		node, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if node.Role != "Master" {
			return nodeConflict("only Master configures resource members")
		}
		member, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), id))
		if errors.Is(err, sql.ErrNoRows) {
			return nodeConflict("unknown resource member")
		}
		if err != nil {
			return err
		}
		if id == node.ID {
			if !c.Storage.Enabled || c.Backup.Enabled {
				return nodeConflict("Master Local storage is mandatory and cannot be a backup target")
			}
		} else {
			var state string
			if member.RelationshipID == nil {
				return nodeConflict("resource member has no relationship")
			}
			if err = q.QueryRowContext(ctx, "SELECT state FROM node_relationships WHERE relationship_id=?"+r.lock(), *member.RelationshipID).Scan(&state); err != nil {
				return err
			}
			if state != "active" {
				return nodeConflict("resource relationship not active")
			}
		}
		if c.Storage.Allocation < member.Used || c.Storage.Allocation-member.Used < member.Reserved {
			return nodeConflict("storage allocation cannot be lower than used and reserved capacity")
		}
		writable := 0
		if c.Storage.Enabled && member.Health == "online" {
			writable = 1
		}
		enabled := 0
		if c.Storage.Enabled {
			enabled = 1
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_storage_members SET storage_enabled=?,allocated_bytes=?,writable=?,updated_at=? WHERE member_id=?", enabled, c.Storage.Allocation, writable, time.Now().Unix(), id); err != nil {
			return err
		}
		if id != node.ID {
			if _, err = q.ExecContext(ctx, "UPDATE cluster_compute_members SET enabled=0,worker_slots=0,available_slots=0,updated_at=? WHERE member_id=?", time.Now().Unix(), id); err != nil {
				return err
			}
		}
		backupEnabled := 0
		if c.Backup.Enabled {
			backupEnabled = 1
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_backup_members SET state=CASE WHEN ?=0 THEN 'disabled' WHEN enabled=0 THEN 'pending' ELSE state END,enabled=?,updated_at=? WHERE member_id=?", backupEnabled, backupEnabled, time.Now().Unix(), id); err != nil {
			return err
		}
		detail := map[string]any{"member_id": id, "storage_enabled": c.Storage.Enabled, "allocated_bytes": c.Storage.Allocation, "compute_enabled": false, "worker_slots": 0, "backup_enabled": c.Backup.Enabled}
		relation := ""
		if member.RelationshipID != nil {
			relation = *member.RelationshipID
		}
		return r.nodeAudit(ctx, q, "resource-member-configured", relation, detail, a)
	})
}
func (r *Repository) MemberConfiguration(ctx context.Context, id string) (c store.ResourceConfiguration, err error) {
	var enabled, backup int
	err = r.db.QueryRowContext(ctx, "SELECT s.storage_enabled,s.allocated_bytes,COALESCE(b.enabled,0) FROM cluster_storage_members s LEFT JOIN cluster_backup_members b ON b.member_id=s.member_id WHERE s.member_id=?", id).Scan(&enabled, &c.Storage.Allocation, &backup)
	if errors.Is(err, sql.ErrNoRows) {
		err = nodeConflict("unknown resource member")
	}
	c.Storage.Enabled = enabled != 0
	c.Backup.Enabled = backup != 0
	return
}
func (r *Repository) AcceptFollowerConfiguration(ctx context.Context, relationID string, c store.ResourceConfiguration, physicalFree int64) error {
	if !validConfiguration(c) || physicalFree < 0 {
		return nodeConflict("invalid resource configuration")
	}
	return r.withRelation(ctx, relationID, func(q queryer, node store.NodeIdentity, v store.Relationship) error {
		if node.Role != "Follower" || v.Direction != "upstream" || v.State != "active" {
			return nodeConflict("only Follower accepts active upstream resource settings")
		}
		current, err := scanMember(q.QueryRowContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members WHERE member_id=?"+r.lock(), node.ID))
		if errors.Is(err, sql.ErrNoRows) {
			current = store.StorageMember{ID: node.ID}
		} else if err != nil {
			return err
		}
		if c.Storage.Allocation < current.Used || c.Storage.Allocation-current.Used < current.Reserved {
			return nodeConflict("storage allocation cannot be lower than used and reserved capacity")
		}
		current.Kind = "Follower"
		current.Transport = "Local"
		current.Allocation = c.Storage.Allocation
		current.PhysicalFree = physicalFree
		current.Health = "online"
		current.Enabled = 0
		current.Writable = 0
		if c.Storage.Enabled {
			current.Enabled = 1
			current.Writable = 1
		}
		current.UpdatedAt = time.Now().Unix()
		if err = r.upsertMember(ctx, q, current, []string{"member_kind", "transport", "storage_enabled", "allocated_bytes", "physical_free_bytes", "health", "writable", "updated_at"}); err != nil {
			return err
		}
		if err = r.initializeAuxMembers(ctx, q, node.ID, current.UpdatedAt); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE cluster_compute_members SET enabled=0,worker_slots=0,available_slots=0,capabilities='[]',updated_at=? WHERE member_id=?", current.UpdatedAt, node.ID); err != nil {
			return err
		}
		backupEnabled := 0
		if c.Backup.Enabled {
			backupEnabled = 1
		}
		_, err = q.ExecContext(ctx, "UPDATE cluster_backup_members SET state=CASE WHEN ?=0 THEN 'disabled' WHEN enabled=0 THEN 'pending' ELSE state END,enabled=?,updated_at=? WHERE member_id=?", backupEnabled, backupEnabled, current.UpdatedAt, node.ID)
		return err
	})
}
func (r *Repository) FollowerSummary(ctx context.Context, physicalFree, physicalTotal int64) (result map[string]any, err error) {
	if physicalFree < 0 || physicalTotal < physicalFree {
		return nil, nodeConflict("invalid physical capacity")
	}
	row, err := r.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if row.Role != "Follower" {
		return nil, nodeConflict("only Follower reports local resource capacity")
	}
	members, err := r.Members(ctx)
	if err != nil {
		return nil, err
	}
	var member store.StorageMember
	found := false
	for _, v := range members {
		if v.ID == row.ID {
			member = v
			found = true
			break
		}
	}
	storage := map[string]any{"enabled": false, "allocated_bytes": int64(0), "used_bytes": int64(0), "reserved_bytes": int64(0), "physical_free_bytes": physicalFree, "physical_total_bytes": physicalTotal, "project_used_bytes": int64(0), "current_allocated_bytes": int64(0)}
	backup := map[string]any{"enabled": false, "generation": 0, "last_success": 0, "lag_seconds": 0, "checksum": "", "state": "disabled", "last_size_bytes": 0, "recovery_points": 0, "last_attempt": 0, "last_attempt_state": "disabled"}
	if found {
		storage["enabled"] = member.Enabled != 0
		storage["allocated_bytes"] = member.Allocation
		storage["used_bytes"] = member.Used
		storage["reserved_bytes"] = member.Reserved
		storage["project_used_bytes"] = member.Used
		storage["current_allocated_bytes"] = member.Allocation
		for k, v := range member.Backup {
			backup[k] = v
		}
		if enabled := peerInteger(backup, "enabled"); enabled != 0 {
			backup["enabled"] = true
			backup["lag_seconds"] = max(0, time.Now().Unix()-peerInteger(backup, "last_success"))
		} else {
			backup["enabled"] = false
		}
		var readyCount int
		if err = r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM cluster_business_backups WHERE state='ready'").Scan(&readyCount); err != nil {
			return nil, err
		}
		backup["recovery_points"] = readyCount
		var size, attempt int64
		var attemptState string
		err = r.db.QueryRowContext(ctx, "SELECT size_bytes FROM cluster_business_backups WHERE state='ready' ORDER BY updated_at DESC LIMIT 1").Scan(&size)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		backup["last_size_bytes"] = size
		err = r.db.QueryRowContext(ctx, "SELECT updated_at,state FROM cluster_business_backups ORDER BY updated_at DESC LIMIT 1").Scan(&attempt, &attemptState)
		if errors.Is(err, sql.ErrNoRows) {
			attemptState = "pending"
		} else if err != nil {
			return nil, err
		}
		backup["last_attempt"] = attempt
		backup["last_attempt_state"] = attemptState
		if _, err = r.db.ExecContext(ctx, "UPDATE cluster_storage_members SET physical_free_bytes=?,updated_at=? WHERE member_id=?", physicalFree, time.Now().Unix(), row.ID); err != nil {
			return nil, err
		}
	}
	return map[string]any{"storage": storage, "compute": map[string]any{"enabled": false, "worker_slots": 0, "available_slots": 0, "cpu_percent": 0, "memory_available_bytes": 0, "capabilities": []string{}}, "backup": backup}, nil
}
func (r *Repository) Members(ctx context.Context) ([]store.StorageMember, error) {
	rows, err := r.db.QueryContext(ctx, "SELECT "+memberColumns+" FROM cluster_storage_members ORDER BY member_kind,member_id")
	if err != nil {
		return nil, err
	}
	result := []store.StorageMember{}
	for rows.Next() {
		v, err := scanMember(rows)
		if err != nil {
			rows.Close()
			return nil, err
		}
		result = append(result, v)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	for i := range result {
		v := &result[i]
		v.Compute = map[string]any{}
		v.Backup = map[string]any{}
		var enabled, slots, available, cpu int
		var memory, updated int64
		var capabilities []byte
		err = r.db.QueryRowContext(ctx, "SELECT enabled,worker_slots,available_slots,cpu_percent,memory_available_bytes,capabilities,updated_at FROM cluster_compute_members WHERE member_id=?", v.ID).Scan(&enabled, &slots, &available, &cpu, &memory, &capabilities, &updated)
		if err == nil {
			var values []string
			if err = json.Unmarshal(capabilities, &values); err != nil {
				return nil, err
			}
			v.Compute = map[string]any{"member_id": v.ID, "enabled": enabled, "worker_slots": slots, "available_slots": available, "cpu_percent": cpu, "memory_available_bytes": memory, "capabilities": values, "updated_at": updated}
		} else if !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		var generation, lastSuccess, lag int64
		var checksum, state string
		err = r.db.QueryRowContext(ctx, "SELECT enabled,generation,last_success,lag_seconds,checksum,state,updated_at FROM cluster_backup_members WHERE member_id=?", v.ID).Scan(&enabled, &generation, &lastSuccess, &lag, &checksum, &state, &updated)
		if err == nil {
			v.Backup = map[string]any{"member_id": v.ID, "enabled": enabled, "generation": generation, "last_success": lastSuccess, "lag_seconds": lag, "checksum": checksum, "state": state, "updated_at": updated}
		} else if !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		if v.RelationshipID != nil {
			relation, err := r.Relationship(ctx, *v.RelationshipID)
			if err != nil {
				return nil, err
			}
			storage, _ := relation.Summary["storage"].(map[string]any)
			v.PhysicalTotal = peerInteger(storage, "physical_total_bytes")
			if relation.State != "active" || relation.Status == "offline" || time.Now().Unix()-relation.LastHeartbeat >= 120 {
				v.Health, v.Writable = "offline", 0
			}
		}
		v.Available = capacity(*v)
		v.OnlineWritable = v.Available
		if v.Health != "online" {
			v.OfflineStored = v.Used
		}
	}
	return result, nil
}

var _ store.PoolRepository = (*Repository)(nil)
