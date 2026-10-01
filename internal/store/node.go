package store

import (
	"context"
	"errors"
)

var ErrNodeState = errors.New("node state conflict")

type NodeAudit struct{ Actor, RequestID, TraceID string }
type PairPackage struct {
	Nonce, TokenHash string
	ExpiresAt        int64
}
type Relationship struct {
	ID            string         `json:"relationship_id"`
	PeerID        string         `json:"peer_id"`
	Endpoint      string         `json:"peer_endpoint"`
	PublicKey     string         `json:"-"`
	Credential    string         `json:"-"` // encrypted, never serialized to Admin clients
	Direction     string         `json:"direction"`
	Mode          string         `json:"mode"`
	State         string         `json:"state"`
	Status        string         `json:"status"`
	LastHeartbeat int64          `json:"last_heartbeat"`
	RTT           int            `json:"rtt_ms"`
	Failures      int            `json:"failures"`
	Recoveries    int            `json:"recoveries"`
	PeerVersion   string         `json:"peer_version"`
	Protocol      int            `json:"protocol"`
	Summary       map[string]any `json:"summary"`
	CreatedAt     int64          `json:"created_at"`
}
type NodeRepository interface {
	InitializeIdentity(context.Context, NodeIdentity) (NodeIdentity, error)
	ReadIdentity(context.Context) (NodeIdentity, error)
	PromoteIdentity(context.Context, NodePromotion, NodeAudit) (NodeIdentity, error)
	IssuePair(context.Context, PairPackage, int64, NodeAudit) (NodeIdentity, error)
	PairAvailable(context.Context, PairPackage, int64) error
	Relationships(context.Context, bool) ([]Relationship, error)
	Relationship(context.Context, string) (Relationship, error)
	PrepareRelationship(context.Context, Relationship, NodeAudit) error
	ConsumePair(context.Context, PairPackage, Relationship, int64, NodeAudit) error
	ActivateRelationship(context.Context, string, NodeAudit) error
	RevokeRelationship(context.Context, string, bool, NodeAudit) error
	AcknowledgeRevocation(context.Context, string, NodeAudit) error
	SetRelationshipMode(context.Context, string, string, bool, NodeAudit) error
	ReserveNodeNonce(context.Context, string, string, string, int64, bool, bool) (Relationship, error)
	RecordHeartbeat(context.Context, string, bool, int, map[string]any, int64) error
}
