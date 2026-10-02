package node

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"fmt"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (s *Service) IdentityState(ctx context.Context) (store.NodeIdentity, error) {
	return s.repo.ReadIdentity(ctx)
}
func (s *Service) Wake() {
	select {
	case s.wakeup <- struct{}{}:
	default:
	}
}
func (s *Service) SetMode(ctx context.Context, id, mode string, a store.NodeAudit) error {
	if err := s.repo.SetRelationshipMode(ctx, id, mode, false, a); err != nil {
		return err
	}
	s.Wake()
	return nil
}
func (s *Service) Configure(ctx context.Context, id string, settings store.ResourceConfiguration, a store.NodeAudit) error {
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	if row.Role != "Master" || s.pool == nil {
		return store.ErrNodeState
	}
	member := row.ID
	if id != row.ID {
		rel, err := s.repo.Relationship(ctx, id)
		if err != nil {
			return err
		}
		if rel.Direction != "downstream" || rel.State != "active" {
			return store.ErrNodeState
		}
		member = rel.PeerID
	}
	if err := s.pool.ConfigureMember(ctx, member, settings, a); err != nil {
		return err
	}
	s.Wake()
	return nil
}

func (s *Service) Reinitialize(ctx context.Context, confirmation string, a store.NodeAudit) (store.NodeIdentity, error) {
	if s.volume == nil {
		return store.NodeIdentity{}, store.ErrNodeState
	}
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return store.NodeIdentity{}, err
	}
	if row.ID != confirmation {
		return store.NodeIdentity{}, fmt.Errorf("%w: 请准确输入当前节点 ID 确认重新初始化", store.ErrNodeState)
	}
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return store.NodeIdentity{}, err
	}
	id, err := randomNodeID()
	if err != nil {
		return store.NodeIdentity{}, err
	}
	sealed, err := s.identity.vault.Seal(protocol.Encode(key.Seed()))
	if err != nil {
		return store.NodeIdentity{}, err
	}
	next := store.NodeIdentity{ID: id, Role: "Standalone", PrivateKey: sealed}
	var result store.NodeIdentity
	// Promotion and reset share the media writer lease. Unknown Follower files
	// and MasterLocal bytes cannot disappear from ownership via an identity reset.
	role := "Master"
	if row.Role == "Follower" {
		role = "Follower"
	}
	err = s.volume.WithPromotion(ctx, role, func(p store.NodePromotion) error {
		if row.Role == "Master" && p.PhysicalUsed != 0 {
			return store.ErrNodeState
		}
		apply := func() error {
			var err error
			result, err = s.repo.ResetIdentity(ctx, confirmation, row.Role, next, a)
			return err
		}
		if row.Role != "Standalone" {
			return s.emptyRecordings(ctx, apply)
		}
		return apply()
	})
	if err != nil {
		return store.NodeIdentity{}, err
	}
	s.Wake()
	return result, nil
}
