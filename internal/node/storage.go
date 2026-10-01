package node

import (
	"context"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"io"
)

func (s *Service) storageRelation(ctx context.Context, v store.UploadReservation) (store.Relationship, error) {
	if v.Member.RelationshipID == nil {
		return store.Relationship{}, ErrCapability
	}
	r, err := s.repo.Relationship(ctx, *v.Member.RelationshipID)
	if err != nil {
		return r, err
	}
	if r.State != "active" || r.Direction != "downstream" || r.PeerID != v.MemberID {
		return r, ErrCapability
	}
	return r, nil
}
func (s *Service) UploadStorage(ctx context.Context, v store.UploadReservation, reader io.Reader) (map[string]any, error) {
	token, err := s.StorageCapability(ctx, v, "upload")
	if err != nil {
		return nil, err
	}
	r, err := s.storageRelation(ctx, v)
	if err != nil {
		return nil, err
	}
	client, ok := s.transport.(StorageClient)
	if !ok {
		return nil, store.ErrNodeState
	}
	return client.StorageUpload(ctx, r.Endpoint, v.MediaID, token, reader, v.ExpectedBytes)
}
func (s *Service) StatStorage(ctx context.Context, v store.UploadReservation) (map[string]any, error) {
	// Capability creation also verifies the current Master identity and protocol.
	if _, err := s.StorageCapability(ctx, v, "upload"); err != nil {
		return nil, err
	}
	r, err := s.storageRelation(ctx, v)
	if err != nil {
		return nil, err
	}
	return s.Call(ctx, r, "/internal/v1/storage/"+v.MediaID+"/stat", map[string]any{"path": v.Path})
}
func (s *Service) DeleteStorage(ctx context.Context, v store.UploadReservation) error {
	token, err := s.StorageCapability(ctx, v, "delete")
	if err != nil {
		return err
	}
	r, err := s.storageRelation(ctx, v)
	if err != nil {
		return err
	}
	result, err := s.Call(ctx, r, "/internal/v1/storage/"+v.MediaID+"/delete?token="+token, map[string]any{})
	if err != nil {
		return err
	}
	if textField(result, "status") != "deleted" {
		return store.ErrNodeState
	}
	return nil
}

func (s *Service) StorageOrigin(ctx context.Context, v store.UploadReservation) (string, error) {
	r, err := s.storageRelation(ctx, v)
	return r.Endpoint, err
}
