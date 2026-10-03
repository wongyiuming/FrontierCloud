package node

import (
	"context"
	"encoding/json"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// The caller holds the closed native lifecycle fence. No new key or account is
// initialized; an existing Master signs only its current database inventory.
func (n *Identity) RecordingInventory(ctx context.Context, repo store.MaintenanceRepository, nodes store.NodeRepository, relationship string, now int64) ([]byte, error) {
	current, err := nodes.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if current.Role != "Master" || current.ID != n.ID || current.PrivateKey != n.PrivateKey {
		return nil, store.ErrNodeState
	}
	inventory, err := repo.ExportRecordingInventory(ctx, relationship, now)
	if err != nil {
		return nil, err
	}
	if inventory.MasterID != current.ID {
		return nil, store.ErrNodeState
	}
	payload, err := inventory.CanonicalPayload()
	if err != nil {
		return nil, err
	}
	key, err := n.vault.Unseal(current.PrivateKey)
	if err != nil {
		return nil, store.ErrNodeState
	}
	signature, err := protocol.Sign(key, payload)
	if err != nil {
		return nil, err
	}
	after, err := nodes.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if after != current {
		return nil, store.ErrNodeState
	}
	raw, err := json.Marshal(store.SignedRecordingInventory{Payload: inventory, Signature: signature})
	if err != nil {
		return nil, err
	}
	if len(raw) > store.MaxRecordingInventoryBytes {
		return nil, store.ErrRecordingState
	}
	return raw, nil
}
