package release

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type Caller interface {
	Call(context.Context, store.Relationship, string, any) (map[string]any, error)
}
type Evidence interface {
	Status(context.Context, bool) (map[string]any, error)
}
type Coordinator struct {
	Agent    Agent
	Verifier Evidence
	Nodes    store.NodeRepository
	Control  Caller
	Policy   Policy
}

func (s *Coordinator) followers(ctx context.Context, identity store.NodeIdentity) ([]map[string]any, error) {
	result := []map[string]any{}
	if identity.Role != "Master" {
		return result, nil
	}
	relations, err := s.Nodes.Relationships(ctx, false)
	if err != nil {
		return nil, err
	}
	var selected []store.Relationship
	for _, r := range relations {
		if r.Direction == "downstream" && r.State == "active" {
			selected = append(selected, r)
		}
	}
	if len(selected) > 1000 {
		return nil, errors.New("release follower selection exceeds 1000")
	}
	result = make([]map[string]any, len(selected))
	slots := make(chan struct{}, 4)
	var workers sync.WaitGroup
	for i, r := range selected {
		select {
		case slots <- struct{}{}:
		case <-ctx.Done():
			workers.Wait()
			return nil, ctx.Err()
		}
		workers.Go(func() {
			defer func() { <-slots }()
			out := map[string]any{"relationship_id": r.ID, "peer_id": r.PeerID, "peer_endpoint": r.Endpoint, "reachable": false, "status": map[string]any{}}
			probe, cancel := context.WithTimeout(ctx, 10*time.Second)
			defer cancel()
			row, err := s.Nodes.ReadIdentity(probe)
			current, relationErr := s.Nodes.Relationship(probe, r.ID)
			if err == nil && relationErr == nil && row.ID == identity.ID && row.Role == "Master" && current.State == "active" && current.Direction == "downstream" && current.PeerID == r.PeerID && current.Endpoint == r.Endpoint && current.PublicKey == r.PublicKey && s.Control != nil {
				value, err := s.Control.Call(probe, current, "/internal/v1/cluster-update/status", map[string]any{})
				if err == nil {
					if status, ok := value["status"].(map[string]any); ok && status != nil {
						out["reachable"], out["status"] = true, status
					}
				}
			}
			if out["reachable"] != true {
				out["detail"] = "Follower updater unavailable"
			}
			result[i] = out
		})
	}
	workers.Wait()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return result, nil
}
func policyReady(policy Policy, local map[string]any, followers []map[string]any) (bool, string) {
	if !policy.Valid() || local["release_branch"] != policy.Branch {
		return false, "local updater release policy mismatch"
	}
	for _, f := range followers {
		status, _ := f["status"].(map[string]any)
		if f["reachable"] != true || status["release_branch"] != policy.Branch {
			return false, "Follower updater unavailable or release policy mismatch"
		}
	}
	return true, ""
}
func needConvergence(followers []map[string]any, target string) bool {
	if !ValidSHA(target) {
		return false
	}
	for _, f := range followers {
		status, _ := f["status"].(map[string]any)
		if f["reachable"] != true || status["current_sha"] != target || status["state"] != "success" {
			return true
		}
	}
	return false
}

func (s *Coordinator) Status(ctx context.Context, refresh bool) (map[string]any, error) {
	n, err := s.Nodes.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	local := AgentStatus(ctx, s.Agent)
	ci, err := s.Verifier.Status(ctx, refresh)
	if err != nil {
		return nil, err
	}
	followers, err := s.followers(ctx, n)
	if err != nil {
		return nil, err
	}
	current, err := s.Nodes.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	ready, detail := policyReady(s.Policy, local, followers)
	if current.ID != n.ID || current.Role != n.Role {
		ready, detail = false, "node identity changed during release inspection"
	}
	n = current
	target, _ := ci["sha"].(string)
	previous, _ := local["previous_sha"].(string)
	convergence := needConvergence(followers, target)
	busy := Busy(local["state"])
	return map[string]any{"role": n.Role, "release_branch": s.Policy.Branch, "ci": ci, "local": local, "followers": followers, "release_policy_ready": ready, "release_policy_detail": detail, "cluster_convergence_needed": convergence, "can_upgrade": n.Role == "Master" && ready && ci["publishable"] == true && ValidSHA(target) && (local["current_sha"] != target || convergence) && !busy, "can_rollback": n.Role == "Master" && ready && ValidSHA(previous) && !busy}, nil
}

func (s *Coordinator) Start(ctx context.Context, mode string) (map[string]any, error) {
	if mode != "upgrade" && mode != "rollback" {
		return nil, errors.New("invalid release mode")
	}
	n, err := s.Nodes.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if n.Role != "Master" {
		return nil, errors.New("only Master can start a cluster release")
	}
	local := AgentStatus(ctx, s.Agent)
	if Busy(local["state"]) {
		return nil, errors.New("release already running")
	}
	if ready, detail := policyReady(s.Policy, local, nil); !ready {
		return nil, errors.New(detail)
	}
	var target string
	if mode == "upgrade" {
		ci, err := s.Verifier.Status(ctx, false)
		if err != nil {
			return nil, err
		}
		target, _ = ci["sha"].(string)
		if ci["publishable"] != true || !ValidSHA(target) {
			return nil, errors.New("release HEAD has no successful reviewed source CI tree")
		}
	} else {
		target, _ = local["previous_sha"].(string)
		if !ValidSHA(target) {
			return nil, errors.New("no previous verified local release")
		}
	}
	followers, err := s.followers(ctx, n)
	if err != nil {
		return nil, err
	}
	if ready, detail := policyReady(s.Policy, local, followers); !ready {
		return nil, errors.New(detail)
	}
	if mode == "upgrade" && local["current_sha"] == target && !needConvergence(followers, target) {
		return nil, errors.New("latest tested release is already converged")
	}
	// Identity/role may change while remote probes were in flight. Never queue
	// a previously-authorized Master's release after its local reset.
	current, err := s.Nodes.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if current.ID != n.ID || current.Role != "Master" {
		return nil, store.ErrNodeState
	}
	if s.Agent == nil {
		return nil, errors.New("updater unavailable")
	}
	out, err := s.Agent.Request(ctx, map[string]any{"action": "start", "target_sha": target, "mode": mode, "hold_maintenance": true})
	if err != nil {
		return nil, err
	}
	if out["ok"] != true {
		return nil, errors.New("updater rejected release")
	}
	return out, nil
}
