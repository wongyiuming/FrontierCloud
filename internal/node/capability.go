package node

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

var ErrCapability = errors.New("resource capability invalid or expired")

func (s *Service) MediaCapability(ctx context.Context, id, owner, objectID, resourceID, requestID, traceID string) (string, error) {
	node, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return "", err
	}
	relation, err := s.repo.Relationship(ctx, id)
	if err != nil {
		return "", err
	}
	if node.Role != "Master" || relation.Direction != "downstream" || relation.State != "active" || relation.Protocol != protocol.Version || relation.PeerID != owner {
		return "", ErrCapability
	}
	credential, err := s.identity.vault.Unseal(relation.Credential)
	if err != nil {
		return "", ErrCapability
	}
	return protocol.MediaToken(credential, relation.ID, node.ID, owner, objectID, resourceID, time.Now().Unix(), requestID, traceID)
}
func capabilityHint(token string) (string, error) {
	if len(token) > 4096 {
		return "", ErrCapability
	}
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return "", ErrCapability
	}
	raw, err := protocol.Decode(parts[0])
	if err != nil {
		return "", ErrCapability
	}
	var hint map[string]any
	if json.Unmarshal(raw, &hint) != nil {
		return "", ErrCapability
	}
	id := textField(hint, "r")
	if !nodeIdentifier.MatchString(id) {
		return "", ErrCapability
	}
	return id, nil
}
func (s *Service) VerifyOwnedMedia(ctx context.Context, token, objectID string) (store.Relationship, map[string]any, error) {
	id, err := capabilityHint(token)
	if err != nil {
		return store.Relationship{}, nil, err
	}
	node, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return store.Relationship{}, nil, err
	}
	relation, err := s.repo.Relationship(ctx, id)
	if err != nil {
		if errors.Is(err, store.ErrNodeState) {
			return store.Relationship{}, nil, ErrCapability
		}
		return store.Relationship{}, nil, err
	}
	credential, err := s.identity.vault.Unseal(relation.Credential)
	if err != nil {
		return store.Relationship{}, nil, ErrCapability
	}
	value, err := protocol.VerifyMediaToken(credential, token, time.Now().Unix())
	if err != nil || node.Role != "Follower" || relation.State != "active" || relation.Direction != "upstream" || relation.Protocol != protocol.Version || textField(value, "o") != node.ID || textField(value, "i") != objectID || textField(value, "m") != relation.PeerID || textField(value, "r") != relation.ID {
		return store.Relationship{}, nil, ErrCapability
	}
	return relation, value, nil
}
func (s *Service) StorageCapability(ctx context.Context, v store.UploadReservation, operation string) (string, error) {
	if v.Member.RelationshipID == nil {
		return "", ErrCapability
	}
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return "", err
	}
	relation, err := s.repo.Relationship(ctx, *v.Member.RelationshipID)
	if err != nil {
		return "", err
	}
	if row.Role != "Master" || relation.State != "active" || relation.Direction != "downstream" || relation.PeerID != v.MemberID || relation.Protocol != protocol.Version {
		return "", ErrCapability
	}
	credential, err := s.identity.vault.Unseal(relation.Credential)
	if err != nil {
		return "", ErrCapability
	}
	return protocol.StorageToken(credential, relation.ID, row.ID, v.MemberID, v.MediaID, v.MediaID, operation, v.Path, v.ExpectedBytes, time.Now().Unix())
}
func (s *Service) VerifyOwnedStorage(ctx context.Context, token, objectID, operation string) (store.Relationship, map[string]any, error) {
	id, err := capabilityHint(token)
	if err != nil {
		return store.Relationship{}, nil, err
	}
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return store.Relationship{}, nil, err
	}
	relation, err := s.repo.Relationship(ctx, id)
	if err != nil {
		if errors.Is(err, store.ErrNodeState) {
			return store.Relationship{}, nil, ErrCapability
		}
		return store.Relationship{}, nil, err
	}
	credential, err := s.identity.vault.Unseal(relation.Credential)
	if err != nil {
		return store.Relationship{}, nil, ErrCapability
	}
	v, err := protocol.VerifyStorageToken(credential, token, time.Now().Unix())
	if err != nil || row.Role != "Follower" || relation.State != "active" || relation.Direction != "upstream" || relation.Protocol != protocol.Version || textField(v, "n") != row.ID || textField(v, "i") != objectID || textField(v, "m") != relation.PeerID || textField(v, "r") != relation.ID || textField(v, "op") != operation {
		return store.Relationship{}, nil, ErrCapability
	}
	return relation, v, nil
}
