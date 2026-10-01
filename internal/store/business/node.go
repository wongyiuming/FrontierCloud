package business

import (
	"bytes"
	"context"
	"crypto/subtle"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

var nodeIDPattern = regexp.MustCompile(`^[a-f0-9]{32}$`)
var nodeHashPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

func nodeConflict(detail string) error { return fmt.Errorf("%w: %s", store.ErrNodeState, detail) }
func readNode(ctx context.Context, q queryer, lock string) (result store.NodeIdentity, err error) {
	err = q.QueryRowContext(ctx, "SELECT node_id,`role`,endpoint,private_key,created_at FROM node_identity WHERE singleton=1"+lock).Scan(&result.ID, &result.Role, &result.Endpoint, &result.PrivateKey, &result.CreatedAt)
	return
}
func (r *Repository) ReadIdentity(ctx context.Context) (store.NodeIdentity, error) {
	return readNode(ctx, r.db, "")
}
func (r *Repository) nodeAudit(ctx context.Context, q queryer, action, id string, detail map[string]any, a store.NodeAudit) error {
	if detail == nil {
		detail = map[string]any{}
	}
	if a.RequestID != "" {
		detail["request_id"] = bounded(a.RequestID, 128)
	}
	if a.TraceID != "" {
		detail["trace_id"] = bounded(a.TraceID, 32)
	}
	encoded, err := json.Marshal(detail)
	if err != nil {
		return err
	}
	random, err := randomID()
	if err != nil {
		return err
	}
	_, err = q.ExecContext(ctx, "INSERT INTO node_audit(audit_id,action,relationship_id,actor,detail,created_at) VALUES (?,?,?,?,?,?)", random[:32], action, optional(id), bounded(a.Actor, 128), string(encoded), time.Now().Unix())
	return err
}
func validPair(p store.PairPackage, now int64) bool {
	return nodeIDPattern.MatchString(p.Nonce) && nodeHashPattern.MatchString(p.TokenHash) && p.ExpiresAt > now && p.ExpiresAt <= now+300
}
func pairAvailable(ctx context.Context, q queryer, p store.PairPackage, now int64, lock string) error {
	var hash, state string
	var expiry int64
	err := q.QueryRowContext(ctx, "SELECT token_hash,state,expires_at FROM node_pair_packages WHERE nonce=?"+lock, p.Nonce).Scan(&hash, &state, &expiry)
	if errors.Is(err, sql.ErrNoRows) {
		return nodeConflict("pairing package unavailable")
	}
	if err != nil {
		return err
	}
	if state != "issued" || expiry <= now || expiry != p.ExpiresAt || subtle.ConstantTimeCompare([]byte(hash), []byte(p.TokenHash)) != 1 {
		return nodeConflict("pairing package unavailable")
	}
	return nil
}
func (r *Repository) PairAvailable(ctx context.Context, p store.PairPackage, now int64) error {
	if !validPair(p, now) {
		return nodeConflict("invalid pairing package")
	}
	return pairAvailable(ctx, r.db, p, now, "")
}
func (r *Repository) IssuePair(ctx context.Context, p store.PairPackage, now int64, a store.NodeAudit) (identity store.NodeIdentity, err error) {
	if !validPair(p, now) {
		return identity, nodeConflict("invalid pairing package")
	}
	err = r.write(ctx, func(q queryer) error {
		var err error
		identity, err = readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if identity.Role != "Follower" {
			return nodeConflict("only Follower can issue pairing packages")
		}
		var count int
		if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM node_relationships WHERE direction='upstream' AND state<>'revoked'").Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return nodeConflict("Follower already follows a Master")
		}
		if _, err = q.ExecContext(ctx, "DELETE FROM node_pair_packages WHERE expires_at<?", now-86400); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "INSERT INTO node_pair_packages(nonce,token_hash,expires_at,state) VALUES (?,?,?,'issued')", p.Nonce, p.TokenHash, p.ExpiresAt); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "pair-issued", "", map[string]any{"nonce": p.Nonce, "expires_at": p.ExpiresAt}, a)
	})
	return
}

const relationColumns = "relationship_id,peer_id,peer_endpoint,peer_key,credential,direction,mode,state,status,last_heartbeat,rtt_ms,failures,recoveries,peer_version,protocol,summary,created_at"

type rowScanner interface{ Scan(...any) error }

func scanRelation(row rowScanner) (v store.Relationship, err error) {
	var summary []byte
	err = row.Scan(&v.ID, &v.PeerID, &v.Endpoint, &v.PublicKey, &v.Credential, &v.Direction, &v.Mode, &v.State, &v.Status, &v.LastHeartbeat, &v.RTT, &v.Failures, &v.Recoveries, &v.PeerVersion, &v.Protocol, &summary, &v.CreatedAt)
	if err != nil {
		return
	}
	decoder := json.NewDecoder(bytes.NewReader(summary))
	decoder.UseNumber()
	err = decoder.Decode(&v.Summary)
	if err == nil && v.Summary == nil {
		v.Summary = map[string]any{}
	}
	return
}
func (r *Repository) Relationship(ctx context.Context, id string) (store.Relationship, error) {
	if !nodeIDPattern.MatchString(id) {
		return store.Relationship{}, nodeConflict("invalid relationship")
	}
	v, err := scanRelation(r.db.QueryRowContext(ctx, "SELECT "+relationColumns+" FROM node_relationships WHERE relationship_id=?", id))
	if errors.Is(err, sql.ErrNoRows) {
		err = nodeConflict("unknown relationship")
	}
	return v, err
}
func (r *Repository) Relationships(ctx context.Context, revoked bool) ([]store.Relationship, error) {
	query := "SELECT " + relationColumns + " FROM node_relationships"
	if !revoked {
		query += " WHERE state<>'revoked'"
	}
	query += " ORDER BY created_at,relationship_id"
	rows, err := r.db.QueryContext(ctx, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []store.Relationship{}
	for rows.Next() {
		v, err := scanRelation(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, v)
	}
	return result, rows.Err()
}
func validRelation(v store.Relationship, direction string) bool {
	return nodeIDPattern.MatchString(v.ID) && nodeIDPattern.MatchString(v.PeerID) && len(v.Endpoint) <= 512 && len(v.Endpoint) > 0 && len(v.PublicKey) == 43 && v.Credential != "" && v.Direction == direction && v.Mode == "Relay" && v.State == "pending" && v.Protocol == 2 && len(v.PeerVersion) <= 64
}
func insertRelation(ctx context.Context, q queryer, v store.Relationship) error {
	_, err := q.ExecContext(ctx, "INSERT INTO node_relationships("+relationColumns+") VALUES (?,?,?,?,?,?,?,'pending','offline',0,0,0,0,?,2,'{}',?)", v.ID, v.PeerID, v.Endpoint, v.PublicKey, v.Credential, v.Direction, v.Mode, v.PeerVersion, v.CreatedAt)
	return err
}
func (r *Repository) PrepareRelationship(ctx context.Context, v store.Relationship, a store.NodeAudit) error {
	if !validRelation(v, "downstream") {
		return nodeConflict("invalid downstream relationship")
	}
	return r.write(ctx, func(q queryer) error {
		node, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if node.Role != "Master" || node.ID == v.PeerID {
			return nodeConflict("only Master can prepare a Follower relationship")
		}
		old, err := scanRelation(q.QueryRowContext(ctx, "SELECT "+relationColumns+" FROM node_relationships WHERE peer_id=?"+r.lock(), v.PeerID))
		if err == nil {
			ack, _ := old.Summary["revocation_acknowledged"].(bool)
			if old.State != "revoked" || !ack {
				return nodeConflict("old relationship not revoked and acknowledged")
			}
			if _, err = q.ExecContext(ctx, "DELETE FROM node_relationships WHERE relationship_id=?", old.ID); err != nil {
				return err
			}
		} else if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if err = insertRelation(ctx, q, v); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "pair-prepared", v.ID, map[string]any{"peer_id": v.PeerID}, a)
	})
}
func (r *Repository) ConsumePair(ctx context.Context, p store.PairPackage, v store.Relationship, now int64, a store.NodeAudit) error {
	if !validPair(p, now) || !validRelation(v, "upstream") {
		return nodeConflict("invalid pairing credentials")
	}
	return r.write(ctx, func(q queryer) error {
		node, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if node.Role != "Follower" || node.ID == v.PeerID {
			return nodeConflict("pair package requires a Follower")
		}
		if err = pairAvailable(ctx, q, p, now, r.lock()); err != nil {
			return err
		}
		var count int
		if err = q.QueryRowContext(ctx, "SELECT COUNT(*) FROM node_relationships WHERE direction='upstream' AND state<>'revoked'").Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return nodeConflict("Follower already follows a Master")
		}
		old, err := scanRelation(q.QueryRowContext(ctx, "SELECT "+relationColumns+" FROM node_relationships WHERE peer_id=?"+r.lock(), v.PeerID))
		if err == nil {
			if old.State != "revoked" {
				return nodeConflict("Master relationship already exists")
			}
			if _, err = q.ExecContext(ctx, "DELETE FROM node_relationships WHERE relationship_id=?", old.ID); err != nil {
				return err
			}
		} else if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if err = insertRelation(ctx, q, v); err != nil {
			return err
		}
		if _, err = q.ExecContext(ctx, "UPDATE node_pair_packages SET state='consumed',relationship_id=?,master_id=? WHERE nonce=?", v.ID, v.PeerID, p.Nonce); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "pair-consumed", v.ID, nil, a)
	})
}
func (r *Repository) withRelation(ctx context.Context, id string, fn func(queryer, store.NodeIdentity, store.Relationship) error) error {
	if !nodeIDPattern.MatchString(id) {
		return nodeConflict("invalid relationship")
	}
	return r.write(ctx, func(q queryer) error {
		node, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		v, err := scanRelation(q.QueryRowContext(ctx, "SELECT "+relationColumns+" FROM node_relationships WHERE relationship_id=?"+r.lock(), id))
		if errors.Is(err, sql.ErrNoRows) {
			return nodeConflict("unknown relationship")
		}
		if err != nil {
			return err
		}
		return fn(q, node, v)
	})
}
func (r *Repository) ActivateRelationship(ctx context.Context, id string, a store.NodeAudit) error {
	return r.withRelation(ctx, id, func(q queryer, node store.NodeIdentity, v store.Relationship) error {
		if v.State != "pending" && v.State != "active" {
			return nodeConflict("relationship revoked")
		}
		if (node.Role != "Master" || v.Direction != "downstream") && (node.Role != "Follower" || v.Direction != "upstream") {
			return nodeConflict("invalid relationship direction")
		}
		if v.State == "active" {
			if node.Role == "Master" {
				return r.registerFollower(ctx, q, v)
			}
			return nil
		}
		if _, err := q.ExecContext(ctx, "UPDATE node_relationships SET state='active' WHERE relationship_id=?", id); err != nil {
			return err
		}
		if node.Role == "Master" {
			v.State = "active"
			if err := r.registerFollower(ctx, q, v); err != nil {
				return err
			}
		}
		return r.nodeAudit(ctx, q, "pair-activated", id, nil, a)
	})
}
func (r *Repository) RevokeRelationship(ctx context.Context, id string, confirmed bool, a store.NodeAudit) error {
	return r.withRelation(ctx, id, func(q queryer, node store.NodeIdentity, v store.Relationship) error {
		var files int
		var query string
		var args []any
		if node.Role == "Follower" {
			query = "SELECT COUNT(*) FROM media_objects WHERE object_kind IN ('audio','video')"
		} else {
			query = "SELECT COUNT(*) FROM global_media_objects WHERE storage_member_id=? AND state IN ('active','pending_delete')"
			args = []any{v.PeerID}
		}
		if err := q.QueryRowContext(ctx, query, args...).Scan(&files); err != nil {
			return err
		}
		var recordings int
		member := v.PeerID
		if node.Role == "Follower" {
			member = node.ID
		}
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM karaoke_recordings WHERE storage_member_id=? AND state IN ('pending','ready','deleting')", member).Scan(&recordings); err != nil {
			return err
		}
		var uploads int
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM cluster_upload_sessions WHERE storage_member_id=? AND state='reserved'", member).Scan(&uploads); err != nil {
			return err
		}
		var reserved int64
		if err := q.QueryRowContext(ctx, "SELECT COALESCE(SUM(reserved_bytes),0) FROM cluster_storage_members WHERE member_id=?", member).Scan(&reserved); err != nil {
			return err
		}
		if files+recordings+uploads > 0 || reserved > 0 {
			return nodeConflict("Follower still holds valid Storage Pool files")
		}
		if confirmed {
			if _, err := q.ExecContext(ctx, "UPDATE node_relationships SET state='revoked',status='offline',summary=? WHERE relationship_id=?", `{"revocation_acknowledged":true}`, id); err != nil {
				return err
			}
		} else {
			if _, err := q.ExecContext(ctx, "UPDATE node_relationships SET state='revoked',status='offline' WHERE relationship_id=?", id); err != nil {
				return err
			}
		}
		if _, err := q.ExecContext(ctx, "UPDATE cluster_storage_members SET health='offline',writable=0,updated_at=? WHERE member_id=?", time.Now().Unix(), member); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "relationship-revoked", id, nil, a)
	})
}
func (r *Repository) AcknowledgeRevocation(ctx context.Context, id string, a store.NodeAudit) error {
	return r.withRelation(ctx, id, func(q queryer, _ store.NodeIdentity, v store.Relationship) error {
		if v.State != "revoked" {
			return nodeConflict("relationship not revoked")
		}
		ack, _ := v.Summary["revocation_acknowledged"].(bool)
		if ack {
			return nil
		}
		if _, err := q.ExecContext(ctx, "UPDATE node_relationships SET summary=? WHERE relationship_id=?", `{"revocation_acknowledged":true}`, id); err != nil {
			return err
		}
		return r.nodeAudit(ctx, q, "revocation-acknowledged", id, nil, a)
	})
}
func (r *Repository) SetRelationshipMode(ctx context.Context, id, mode string, accepted bool, a store.NodeAudit) error {
	if mode != "Direct" && mode != "Relay" {
		return nodeConflict("invalid transport mode")
	}
	return r.withRelation(ctx, id, func(q queryer, node store.NodeIdentity, v store.Relationship) error {
		role, direction, action := "Master", "downstream", "mode-changed"
		if accepted {
			role, direction, action = "Follower", "upstream", "mode-accepted"
		}
		if node.Role != role || v.Direction != direction || v.State != "active" {
			return nodeConflict("no active relationship with correct direction")
		}
		if _, err := q.ExecContext(ctx, "UPDATE node_relationships SET mode=? WHERE relationship_id=?", mode, id); err != nil {
			return err
		}
		if !accepted {
			v.Mode = mode
			if err := r.registerFollower(ctx, q, v); err != nil {
				return err
			}
		}
		return r.nodeAudit(ctx, q, action, id, map[string]any{"mode": mode}, a)
	})
}
func (r *Repository) ReserveNodeNonce(ctx context.Context, id, nonce, expectedCredential string, now int64, pending, revoked bool) (result store.Relationship, err error) {
	if !nodeIDPattern.MatchString(nonce) {
		return result, nodeConflict("invalid request nonce")
	}
	err = r.withRelation(ctx, id, func(q queryer, _ store.NodeIdentity, v store.Relationship) error {
		if subtle.ConstantTimeCompare([]byte(expectedCredential), []byte(v.Credential)) != 1 {
			return nodeConflict("relationship credentials changed")
		}
		if v.Protocol != 2 || (v.State != "active" && !(pending && v.State == "pending") && !(revoked && v.State == "revoked")) {
			return nodeConflict("relationship not active")
		}
		if _, err := q.ExecContext(ctx, "DELETE FROM node_request_nonces WHERE expires_at<=?", now); err != nil {
			return err
		}
		var count int
		if err := q.QueryRowContext(ctx, "SELECT COUNT(*) FROM node_request_nonces WHERE relationship_id=? AND nonce=?", id, nonce).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return nodeConflict("replayed relationship request")
		}
		if _, err := q.ExecContext(ctx, "INSERT INTO node_request_nonces(relationship_id,nonce,expires_at) VALUES (?,?,?)", id, nonce, now+121); err != nil {
			return err
		}
		result = v
		return nil
	})
	return
}
func (r *Repository) RecordHeartbeat(ctx context.Context, id string, success bool, rtt int, summary map[string]any, now int64) error {
	return r.withRelation(ctx, id, func(q queryer, node store.NodeIdentity, v store.Relationship) error {
		if v.State != "active" {
			return nil
		}
		status := v.Status
		if success {
			if rtt < 0 {
				rtt = 0
			}
			recovered := v.Status != "online" && v.LastHeartbeat != 0
			recoveries := v.Recoveries
			if recovered {
				recoveries++
			}
			copy := map[string]any{}
			for k, v := range summary {
				copy[k] = v
			}
			copy["recovered_at"] = v.Summary["recovered_at"]
			if copy["recovered_at"] == nil {
				copy["recovered_at"] = 0
			}
			if recovered {
				copy["recovered_at"] = now
			}
			encoded, err := json.Marshal(copy)
			if err != nil {
				return err
			}
			if len(encoded) > MaxNodeSummaryBytes {
				return nodeConflict("heartbeat summary too large")
			}
			status = "online"
			if _, err = q.ExecContext(ctx, "UPDATE node_relationships SET status=?,failures=0,last_heartbeat=?,rtt_ms=?,recoveries=?,summary=? WHERE relationship_id=?", status, now, rtt, recoveries, string(encoded), id); err != nil {
				return err
			}
			v.Summary = copy
		} else {
			status = "degraded"
			if now-v.LastHeartbeat >= 120 {
				status = "offline"
			}
			if _, err := q.ExecContext(ctx, "UPDATE node_relationships SET failures=failures+1,status=? WHERE relationship_id=?", status, id); err != nil {
				return err
			}
		}
		previous := v.Status
		v.Status = status
		if node.Role == "Master" && v.Direction == "downstream" {
			if err := r.registerFollower(ctx, q, v); err != nil {
				return err
			}
		}
		if status != previous {
			return r.nodeAudit(ctx, q, "relationship-status", id, map[string]any{"previous": previous, "current": status}, store.NodeAudit{Actor: "heartbeat"})
		}
		return nil
	})
}

const MaxNodeSummaryBytes = 512 * 1024
