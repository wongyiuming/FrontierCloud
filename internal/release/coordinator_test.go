package release

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type fixtureNodes struct {
	store.NodeRepository
	mu        sync.Mutex
	identity  store.NodeIdentity
	relations []store.Relationship
}

func (n *fixtureNodes) ReadIdentity(ctx context.Context) (store.NodeIdentity, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.identity, ctx.Err()
}
func (n *fixtureNodes) Relationships(ctx context.Context, _ bool) ([]store.Relationship, error) {
	return n.relations, ctx.Err()
}
func (n *fixtureNodes) Relationship(ctx context.Context, id string) (store.Relationship, error) {
	for _, r := range n.relations {
		if r.ID == id {
			return r, ctx.Err()
		}
	}
	return store.Relationship{}, store.ErrNodeState
}

type fixtureAgent struct {
	local   map[string]any
	started []map[string]any
}

func (a *fixtureAgent) Request(_ context.Context, value map[string]any) (map[string]any, error) {
	if value["action"] == "status" {
		return map[string]any{"ok": true, "status": copyValue(a.local)}, nil
	}
	a.started = append(a.started, copyValue(value))
	return map[string]any{"ok": true, "accepted": true}, nil
}

type fixtureEvidence struct {
	value map[string]any
	calls int
}

func (e *fixtureEvidence) Status(_ context.Context, _ bool) (map[string]any, error) {
	e.calls++
	return copyValue(e.value), nil
}

type fixtureCaller struct {
	status  map[string]any
	offline bool
	before  func()
	calls   int
}

func (c *fixtureCaller) Call(ctx context.Context, r store.Relationship, route string, value any) (map[string]any, error) {
	c.calls++
	if c.before != nil {
		c.before()
	}
	if c.offline {
		return nil, errors.New("private connection detail must not leak")
	}
	if route != "/internal/v1/cluster-update/status" || r.State != "active" {
		return nil, errors.New("invalid follower probe")
	}
	return map[string]any{"status": copyValue(c.status)}, ctx.Err()
}

func TestReleaseCoordinatorAuthorizationPolicyCIConvergenceAndRollback(t *testing.T) {
	target, current, previous := strings.Repeat("b", 40), strings.Repeat("a", 40), strings.Repeat("c", 40)
	for _, kind := range []string{"upgrade", "rollback", "pending-ci", "offline", "policy-mismatch", "converged", "retry-convergence", "busy", "Follower", "Standalone", "no-previous"} {
		t.Run(kind, func(t *testing.T) {
			nodes := &fixtureNodes{identity: store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Master"}, relations: []store.Relationship{{ID: strings.Repeat("d", 32), PeerID: strings.Repeat("e", 32), Direction: "downstream", State: "active", Endpoint: "https://peer.test", PublicKey: "pin"}}}
			agent := &fixtureAgent{local: map[string]any{"release_branch": "main", "current_sha": current, "previous_sha": previous, "state": "success"}}
			evidence := &fixtureEvidence{value: map[string]any{"sha": target, "publishable": true}}
			caller := &fixtureCaller{status: map[string]any{"release_branch": "main", "current_sha": target, "state": "success"}}
			mode, want := "upgrade", true
			switch kind {
			case "rollback":
				mode = "rollback"
			case "pending-ci":
				evidence.value["publishable"] = false
				want = false
			case "offline":
				caller.offline = true
				want = false
			case "policy-mismatch":
				caller.status["release_branch"] = "gin_main"
				want = false
			case "converged":
				agent.local["current_sha"] = target
				want = false
			case "retry-convergence":
				agent.local["current_sha"] = target
				caller.status["current_sha"] = current
			case "busy":
				agent.local["state"] = "running"
				want = false
			case "Follower", "Standalone":
				nodes.identity.Role = kind
				want = false
			case "no-previous":
				agent.local["previous_sha"] = ""
				mode = "rollback"
				want = false
			}
			service := &Coordinator{Agent: agent, Verifier: evidence, Nodes: nodes, Control: caller, Policy: DefaultPolicy()}
			out, err := service.Start(context.Background(), mode)
			if (err == nil) != want || want && out["accepted"] != true || len(agent.started) != map[bool]int{false: 0, true: 1}[want] {
				t.Fatal(kind, out, err, agent.started)
			}
			if mode == "rollback" && evidence.calls != 0 {
				t.Fatal("rollback spent CI quota")
			}
			if want {
				request := agent.started[0]
				selected := target
				if mode == "rollback" {
					selected = previous
				}
				if request["target_sha"] != selected || request["hold_maintenance"] != true {
					t.Fatal("invalid cluster release intent", request)
				}
			}
			if err != nil && strings.Contains(err.Error(), "private connection") {
				t.Fatal("private transport detail exposed")
			}
		})
	}
}

func TestReleaseCoordinatorResetDuringProbeCannotQueueOrAdvertiseRelease(t *testing.T) {
	for _, operation := range []string{"start", "status"} {
		t.Run(operation, func(t *testing.T) {
			n := &fixtureNodes{identity: store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Master"}, relations: []store.Relationship{{ID: strings.Repeat("d", 32), PeerID: strings.Repeat("e", 32), State: "active", Direction: "downstream"}}}
			a := &fixtureAgent{local: map[string]any{"release_branch": "main", "current_sha": strings.Repeat("a", 40), "state": "success"}}
			c := &fixtureCaller{status: map[string]any{"release_branch": "main", "current_sha": strings.Repeat("b", 40), "state": "success"}, before: func() {
				n.mu.Lock()
				n.identity.Role = "Standalone"
				n.identity.ID = strings.Repeat("f", 32)
				n.mu.Unlock()
			}}
			s := &Coordinator{Agent: a, Nodes: n, Control: c, Verifier: &fixtureEvidence{value: map[string]any{"sha": strings.Repeat("b", 40), "publishable": true}}, Policy: DefaultPolicy()}
			if operation == "start" {
				if _, err := s.Start(context.Background(), "upgrade"); !errors.Is(err, store.ErrNodeState) {
					t.Fatal("reset release accepted", err)
				}
			} else {
				value, err := s.Status(context.Background(), false)
				if err != nil || value["can_upgrade"] != false || value["role"] != "Standalone" {
					t.Fatal("stale authority advertised", value, err)
				}
			}
			if len(a.started) != 0 {
				t.Fatal("reset node queued release")
			}
		})
	}
}
