package release

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"sync"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func ManifestFromValue(value any) (Manifest, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return Manifest{}, ErrManifest
	}
	return ParseManifest(raw)
}
func ManifestPeerStatus(value map[string]any, manifest Manifest) (Policy, error) {
	caps, err := protocol.ReadCapabilities(value)
	if err != nil || !slices.Contains(caps, ManifestCapability) {
		return Policy{}, ErrManifest
	}
	status, ok := value["status"].(map[string]any)
	if !ok {
		return Policy{}, ErrManifest
	}
	branch, _ := status["release_branch"].(string)
	policy, err := PolicyForBranch(branch)
	if err != nil {
		return Policy{}, ErrManifest
	}
	if _, err = manifest.Select(policy); err != nil {
		return Policy{}, ErrManifest
	}
	return policy, nil
}

// All peers receive the whole manifest. Preflight precedes dispatch; convergence
// requires its common digest AND each peer's privately selected artifact SHA.
func (s *Coordinator) ConvergeManifest(parent context.Context, manifest Manifest, mode string) error {
	return s.convergeManifest(parent, manifest, mode, 4*time.Second)
}
func (s *Coordinator) convergeManifest(parent context.Context, manifest Manifest, mode string, interval time.Duration) error {
	if !manifest.Valid() || (mode != "upgrade" && mode != "rollback") || !s.Policy.Valid() || s.Control == nil {
		return ErrManifest
	}
	immutable := CloneManifest(&manifest)
	if immutable == nil {
		return ErrManifest
	}
	manifest = *immutable
	id, _ := manifest.ID()
	wire, _ := manifest.Wire()
	whole, _ := protocol.ParseStrictJSON(wire, MaxManifestBytes)
	ctx, cancel := context.WithTimeout(parent, 15*time.Minute)
	defer cancel()
	identity, err := s.Nodes.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	if identity.Role != "Master" {
		return store.ErrNodeState
	}
	rows, err := s.Nodes.Relationships(ctx, false)
	if err != nil {
		return err
	}
	peers := map[string]store.Relationship{}
	for _, r := range rows {
		if r.Direction == "downstream" && r.State == "active" {
			peers[r.ID] = r
		}
	}
	if len(peers) > 1000 {
		return errors.New("too many release followers")
	}
	check := func() error {
		current, err := s.Nodes.ReadIdentity(ctx)
		if err != nil {
			return err
		}
		if current.ID != identity.ID || current.Role != "Master" {
			return store.ErrNodeState
		}
		rows, err := s.Nodes.Relationships(ctx, false)
		if err != nil {
			return err
		}
		count := 0
		for _, r := range rows {
			if r.Direction != "downstream" || r.State != "active" {
				continue
			}
			count++
			old, ok := peers[r.ID]
			if !ok || !sameReleasePeer(old, r) {
				return store.ErrNodeState
			}
		}
		if count != len(peers) {
			return store.ErrNodeState
		}
		return nil
	}
	if err = check(); err != nil {
		return err
	}
	policies := map[string]Policy{}
	var policyLock sync.Mutex
	err = s.parallel(ctx, peers, func(ctx context.Context, r store.Relationship) error {
		if err := check(); err != nil {
			return err
		}
		current, err := s.Nodes.Relationship(ctx, r.ID)
		if err != nil || !sameReleasePeer(r, current) {
			return store.ErrNodeState
		}
		value, err := s.Control.Call(ctx, current, "/internal/v1/cluster-update/status", map[string]any{})
		if err != nil {
			return err
		}
		policy, err := ManifestPeerStatus(value, manifest)
		if err != nil {
			return errors.New("Follower lacks manifest capability or compatible private profile")
		}
		policyLock.Lock()
		policies[r.ID] = policy
		policyLock.Unlock()
		return nil
	})
	if err != nil {
		return err
	}
	if err = check(); err != nil {
		return err
	}
	err = s.parallel(ctx, peers, func(ctx context.Context, r store.Relationship) error {
		if err := check(); err != nil {
			return err
		}
		current, err := s.Nodes.Relationship(ctx, r.ID)
		if err != nil || !sameReleasePeer(r, current) {
			return store.ErrNodeState
		}
		value, err := s.Control.Call(ctx, current, "/internal/v1/cluster-update/start", map[string]any{"release_manifest": whole, "mode": mode})
		if err != nil || value["accepted"] != true || value["release_id"] != id {
			return errors.New("Follower manifest request not acknowledged")
		}
		return nil
	})
	if err != nil {
		return err
	}
	for {
		if err = check(); err != nil {
			return err
		}
		complete := true
		var resultLock sync.Mutex
		err = s.parallel(ctx, peers, func(ctx context.Context, r store.Relationship) error {
			current, err := s.Nodes.Relationship(ctx, r.ID)
			if err != nil || !sameReleasePeer(r, current) {
				return store.ErrNodeState
			}
			value, err := s.Control.Call(ctx, current, "/internal/v1/cluster-update/status", map[string]any{})
			if err != nil {
				resultLock.Lock()
				complete = false
				resultLock.Unlock()
				return nil
			}
			policy, err := ManifestPeerStatus(value, manifest)
			if err != nil || policy != policies[r.ID] {
				return ErrManifest
			}
			status := value["status"].(map[string]any)
			if target, e := ManifestFromValue(status["target_manifest"]); e == nil {
				targetID, _ := target.ID()
				if targetID == id && status["state"] == "failed" {
					return errors.New("Follower manifest release failed; committed local generation retained")
				}
			}
			committed, e := ManifestFromValue(status["current_manifest"])
			currentID := ""
			if e == nil {
				currentID, _ = committed.ID()
			}
			artifact, _ := manifest.Select(policy)
			if status["state"] != "success" || currentID != id || status["current_sha"] != artifact.CommitSHA {
				resultLock.Lock()
				complete = false
				resultLock.Unlock()
			}
			return nil
		})
		if err != nil {
			return err
		}
		if complete {
			return check()
		}
		select {
		case <-ctx.Done():
			return errors.New("manifest convergence deadline reached")
		case <-time.After(interval):
		}
	}
}
