package store

import (
	"encoding/json"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
)

const MaxRecordingInventoryBytes = 4 * 1024 * 1024
const MaxRecordingInventoryItems = 5000

// Offline physical ownership proof, not a new account or quota authority.
// Secrets, credentials and mutable business metadata are deliberately absent.
type RecordingProof struct {
	ID          string `json:"recording_id"`
	UserID      string `json:"user_id"`
	Filename    string `json:"filename"`
	ContentType string `json:"content_type"`
	Bytes       int64  `json:"size_bytes"`
	SHA256      string `json:"sha256"`
	CreatedAt   int64  `json:"created_at"`
}
type RecordingInventory struct {
	Kind             string           `json:"kind"`
	Version          int              `json:"version"`
	SchemaGeneration int              `json:"schema_generation"`
	MasterID         string           `json:"master_id"`
	FollowerID       string           `json:"follower_id"`
	Relationship     string           `json:"relationship_id"`
	CreatedAt        int64            `json:"created_at"`
	ExpiresAt        int64            `json:"expires_at"`
	Recordings       []RecordingProof `json:"recordings"`
}
type SignedRecordingInventory struct {
	Payload   RecordingInventory `json:"payload"`
	Signature string             `json:"signature"`
}

// Canonical signing preserves integer precision and rejects oversize manifests.
func (v RecordingInventory) CanonicalPayload() (any, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return protocol.ParseStrictJSON(raw, MaxRecordingInventoryBytes)
}
