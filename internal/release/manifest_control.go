package release

import (
	"context"
	"errors"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (s *Coordinator) verifyJointRelease(ctx context.Context, m Manifest, mode string) error {
	if !m.Valid() || mode != "upgrade" && mode != "rollback" {
		return ErrManifest
	}
	for _, branch := range []string{"main", "gin_main"} {
		policy, _ := PolicyForBranch(branch)
		verifier := s.ManifestEvidence[branch]
		if verifier == nil {
			return ErrManifest
		}
		proof, err := verifier.Artifact(ctx, m.Artifacts[branch].CommitSHA)
		if err != nil {
			return err
		}
		if err = m.CheckEvidence(policy, proof); err != nil {
			return err
		}
		if mode == "upgrade" {
			// A valid historical artifact cannot become a new HEAD release by
			// relabeling a local file. Both publication profiles must be current.
			head, ok := verifier.(Evidence)
			if !ok {
				return ErrManifest
			}
			current, err := head.Status(ctx, true)
			if err != nil {
				return err
			}
			if err = m.CheckEvidence(policy, current); err != nil {
				return err
			}
		}
	}
	return nil
}

func (s *Coordinator) manifestSelection(ctx context.Context, identity store.NodeIdentity) (map[string]store.Relationship, error) {
	current, err := s.Nodes.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if current.ID != identity.ID || current.Role != "Master" {
		return nil, store.ErrNodeState
	}
	rows, err := s.Nodes.Relationships(ctx, false)
	if err != nil {
		return nil, err
	}
	peers := map[string]store.Relationship{}
	for _, r := range rows {
		if r.Direction == "downstream" && r.State == "active" {
			peers[r.ID] = r
		}
	}
	if len(peers) > 1000 {
		return nil, ErrManifest
	}
	return peers, nil
}
func (s *Coordinator) checkManifestSelection(ctx context.Context, identity store.NodeIdentity, peers map[string]store.Relationship) error {
	current, err := s.manifestSelection(ctx, identity)
	if err != nil {
		return err
	}
	if len(peers) != len(current) {
		return store.ErrNodeState
	}
	for id, old := range peers {
		if !sameReleasePeer(old, current[id]) {
			return store.ErrNodeState
		}
	}
	return nil
}
func jointReady(policy Policy, local map[string]any, followers []map[string]any, m Manifest) bool {
	private, err := ManifestPeerStatus(local, m)
	if err != nil || private != policy {
		return false
	}
	for _, follower := range followers {
		if follower["reachable"] != true {
			return false
		}
		if _, err := ManifestPeerStatus(follower, m); err != nil {
			return false
		}
	}
	return true
}
func manifestConverged(value map[string]any, m Manifest) bool {
	policy, err := ManifestPeerStatus(value, m)
	if err != nil {
		return false
	}
	status, _ := value["status"].(map[string]any)
	current, err := ManifestFromValue(status["current_manifest"])
	if err != nil {
		return false
	}
	id, _ := current.ID()
	wanted, _ := m.ID()
	return id == wanted && status["current_sha"] == m.Artifacts[policy.Branch].CommitSHA && status["state"] == "success"
}

func (s *Coordinator) startPublishedManifest(ctx context.Context, mode string) (map[string]any, error) {
	if mode != "upgrade" && mode != "rollback" || !s.Policy.Valid() {
		return nil, ErrManifest
	}
	identity, err := s.Nodes.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	peers, err := s.manifestSelection(ctx, identity)
	if err != nil {
		return nil, err
	}
	local := AgentControlStatus(ctx, s.Agent)
	status, _ := local["status"].(map[string]any)
	if Busy(status["state"]) {
		return nil, errors.New("release already running")
	}
	var manifest Manifest
	if mode == "upgrade" {
		manifest, err = s.ManifestSource.Latest(ctx)
	} else {
		manifest, err = ManifestFromValue(status["previous_manifest"])
	}
	if err != nil {
		return nil, ErrManifest
	}
	if err = s.verifyJointRelease(ctx, manifest, mode); err != nil {
		return nil, err
	}
	followers, err := s.followers(ctx, identity)
	if err != nil {
		return nil, err
	}
	if !jointReady(s.Policy, local, followers, manifest) {
		return nil, errors.New("updater lacks manifest capability or compatible private profile")
	}
	converged := manifestConverged(local, manifest)
	for _, follower := range followers {
		converged = converged && manifestConverged(follower, manifest)
	}
	if mode == "upgrade" && converged {
		return nil, errors.New("published whole release already converged")
	}
	if err = s.checkManifestSelection(ctx, identity, peers); err != nil {
		return nil, err
	}
	// A status-history change during proof/probes cannot choose an unrelated
	// previous joint release; the agent still serializes the actual queue.
	fresh := AgentControlStatus(ctx, s.Agent)
	freshStatus, _ := fresh["status"].(map[string]any)
	if Busy(freshStatus["state"]) || !jointReady(s.Policy, fresh, nil, manifest) {
		return nil, ErrManifest
	}
	if mode == "rollback" {
		previous, e := ManifestFromValue(freshStatus["previous_manifest"])
		id, _ := previous.ID()
		wanted, _ := manifest.ID()
		if e != nil || id != wanted {
			return nil, ErrManifest
		}
	}
	if err = s.checkManifestSelection(ctx, identity, peers); err != nil {
		return nil, err
	}
	wire, _ := manifest.Wire()
	whole, _ := protocol.ParseStrictJSON(wire, MaxManifestBytes)
	out, err := s.Agent.Request(ctx, map[string]any{"action": "start", "release_manifest": whole, "mode": mode, "hold_maintenance": true})
	id, _ := manifest.ID()
	if err != nil {
		return nil, err
	}
	if out["ok"] != true || out["release_id"] != id {
		return nil, errors.New("updater did not acknowledge whole manifest")
	}
	return out, nil
}

func (s *Coordinator) manifestStatus(ctx context.Context) (map[string]any, error) {
	identity, err := s.Nodes.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	local := AgentControlStatus(ctx, s.Agent)
	status, _ := local["status"].(map[string]any)
	followers, err := s.followers(ctx, identity)
	if err != nil {
		return nil, err
	}
	m, manifestErr := s.ManifestSource.Latest(ctx)
	verified := manifestErr == nil && s.verifyJointRelease(ctx, m, "upgrade") == nil
	ready := manifestErr == nil && jointReady(s.Policy, local, followers, m)
	fresh, err := s.Nodes.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if fresh.ID != identity.ID || fresh.Role != identity.Role {
		ready = false
	}
	converged := manifestConverged(local, m)
	for _, f := range followers {
		converged = converged && manifestConverged(f, m)
	}
	previous, previousErr := ManifestFromValue(status["previous_manifest"])
	rollbackReady := previousErr == nil && jointReady(s.Policy, local, followers, previous)
	ci := map[string]any{"available": manifestErr == nil, "publishable": verified, "branch": s.Policy.Branch}
	var whole any
	if manifestErr == nil {
		ci["sha"] = m.Artifacts[s.Policy.Branch].CommitSHA
		wire, _ := m.Wire()
		whole, _ = protocol.ParseStrictJSON(wire, MaxManifestBytes)
	}
	return map[string]any{"role": fresh.Role, "release_branch": s.Policy.Branch, "ci": ci, "local": status, "followers": followers, "release_manifest": whole, "release_policy_ready": ready, "release_policy_detail": "whole-manifest publication requires both exact reviewed CI proofs and compatible updater capabilities", "cluster_convergence_needed": !converged,
		"can_upgrade": fresh.Role == "Master" && ready && verified && !converged && !Busy(status["state"]), "can_rollback": fresh.Role == "Master" && rollbackReady && !Busy(status["state"])}, nil
}
