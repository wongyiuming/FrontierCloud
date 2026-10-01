package node

import (
	"context"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"io"
	"time"
)

func (s *Service) RecordingCapability(ctx context.Context, v store.Recording, m store.StorageMember, operation string) (string, error) {
	row, e := s.repo.ReadIdentity(ctx)
	if e != nil {
		return "", e
	}
	if row.Role != "Master" || m.RelationshipID == nil || m.ID != v.MemberID {
		return "", ErrCapability
	}
	rel, e := s.repo.Relationship(ctx, *m.RelationshipID)
	if e != nil {
		return "", e
	}
	if rel.State != "active" || rel.Direction != "downstream" || rel.PeerID != v.MemberID || rel.Protocol != protocol.Version {
		return "", ErrCapability
	}
	credential, e := s.identity.vault.Unseal(rel.Credential)
	if e != nil {
		return "", ErrCapability
	}
	return protocol.RecordingToken(credential, rel.ID, row.ID, v.UserID, v.ID, operation, time.Now().Unix(), v.Bytes, v.ContentType, v.Filename)
}
func (s *Service) VerifyOwnedRecording(ctx context.Context, token, id, operation string) (store.Relationship, map[string]any, error) {
	relID, e := capabilityHint(token)
	if e != nil {
		return store.Relationship{}, nil, e
	}
	row, e := s.repo.ReadIdentity(ctx)
	if e != nil {
		return store.Relationship{}, nil, e
	}
	rel, e := s.repo.Relationship(ctx, relID)
	if e != nil {
		if errors.Is(e, store.ErrNodeState) {
			e = ErrCapability
		}
		return store.Relationship{}, nil, e
	}
	credential, e := s.identity.vault.Unseal(rel.Credential)
	if e != nil {
		return store.Relationship{}, nil, ErrCapability
	}
	value, e := protocol.VerifyRecordingToken(credential, token, time.Now().Unix())
	op := textField(value, "op")
	validOp := op == operation
	if operation == "read" {
		validOp = op == "stream" || op == "download"
	}
	if e != nil || row.Role != "Follower" || rel.State != "active" || rel.Direction != "upstream" || rel.Protocol != protocol.Version || textField(value, "r") != rel.ID || textField(value, "m") != rel.PeerID || textField(value, "i") != id || !validOp || !store.ValidRecordingFilename(textField(value, "name")) {
		return store.Relationship{}, nil, ErrCapability
	}
	return rel, value, nil
}
func (s *Service) RecordingRelation(ctx context.Context, v store.Recording, m store.StorageMember) (store.Relationship, error) {
	if _, e := s.RecordingCapability(ctx, v, m, "stream"); e != nil {
		return store.Relationship{}, e
	}
	return s.repo.Relationship(ctx, *m.RelationshipID)
}
func (s *Service) UploadRecording(ctx context.Context, v store.Recording, m store.StorageMember, reader io.Reader) (map[string]any, error) {
	token, e := s.RecordingCapability(ctx, v, m, "upload")
	if e != nil {
		return nil, e
	}
	rel, e := s.RecordingRelation(ctx, v, m)
	if e != nil {
		return nil, e
	}
	client, ok := s.transport.(RecordingClient)
	if !ok {
		return nil, store.ErrNodeState
	}
	return client.RecordingUpload(ctx, rel.Endpoint, v.ID, token, v.ContentType, reader, v.Bytes)
}
func (s *Service) StatRecording(ctx context.Context, v store.Recording, m store.StorageMember) (map[string]any, error) {
	rel, e := s.RecordingRelation(ctx, v, m)
	if e != nil {
		return nil, e
	}
	return s.Call(ctx, rel, "/internal/v1/recordings/"+v.ID+"/stat", map[string]any{"user_id": v.UserID})
}
func (s *Service) DeleteRecording(ctx context.Context, v store.Recording, m store.StorageMember) error {
	rel, e := s.RecordingRelation(ctx, v, m)
	if e != nil {
		return e
	}
	result, e := s.Call(ctx, rel, "/internal/v1/recordings/"+v.ID+"/delete", map[string]any{"user_id": v.UserID})
	if e != nil {
		return e
	}
	if textField(result, "status") != "deleted" {
		return store.ErrRecordingState
	}
	return nil
}
