package node

import (
	"context"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// DiagnosticUpstream uses current SQL state, never the startup role or a cached
// revoked credential. The caller still signs through Call/Authenticate.
func (s *Service) DiagnosticUpstream(ctx context.Context) (*store.Relationship, error) {
	identity, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if identity.Role != "Follower" {
		return nil, nil
	}
	relations, err := s.repo.Relationships(ctx, false)
	if err != nil {
		return nil, err
	}
	for _, r := range relations {
		if r.Direction == "upstream" && r.State == "active" {
			return &r, nil
		}
	}
	return nil, nil
}
