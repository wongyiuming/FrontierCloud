package release

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type convergenceCaller struct {
	mu                              sync.Mutex
	active, maximum, starts, probes int
	branch, target                  string
	failed, offline, unacknowledged bool
	before                          func()
}

func (c *convergenceCaller) Call(ctx context.Context, r store.Relationship, route string, value any) (map[string]any, error) {
	c.mu.Lock()
	c.active++
	if c.active > c.maximum {
		c.maximum = c.active
	}
	c.mu.Unlock()
	defer func() { c.mu.Lock(); c.active--; c.mu.Unlock() }()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(time.Millisecond):
	}
	if c.before != nil {
		c.before()
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.offline {
		return nil, errors.New("offline")
	}
	if route == "/internal/v1/cluster-update/start" {
		c.starts++
		return map[string]any{"accepted": !c.unacknowledged}, nil
	}
	c.probes++
	state := "success"
	if c.failed {
		state = "failed"
	}
	return map[string]any{"status": map[string]any{"current_sha": c.target, "target_sha": c.target, "state": state, "release_branch": c.branch}}, nil
}
func TestClusterConvergenceBoundedFreshAuthorityAndFailure(t *testing.T) {
	for _, kind := range []string{"success", "no-followers", "follower", "failed", "offline", "unacknowledged", "reset", "policy-mismatch"} {
		t.Run(kind, func(t *testing.T) {
			n := &fixtureNodes{identity: store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Master"}}
			for i := range 12 {
				n.relations = append(n.relations, store.Relationship{ID: fmt.Sprintf("%032x", i+1), PeerID: fmt.Sprintf("%032x", 100+i), PublicKey: "pin", Endpoint: "https://peer.test", Protocol: 2, Direction: "downstream", State: "active"})
			}
			c := &convergenceCaller{target: strings.Repeat("b", 40), branch: "main"}
			want := true
			switch kind {
			case "no-followers":
				n.relations = nil
			case "follower":
				n.identity.Role = "Follower"
				want = false
			case "failed":
				c.failed = true
				want = false
			case "offline":
				c.offline = true
				want = false
			case "unacknowledged":
				c.unacknowledged = true
				want = false
			case "reset":
				c.before = func() { n.mu.Lock(); n.identity.Role = "Standalone"; n.mu.Unlock() }
				want = false
			case "policy-mismatch":
				c.branch = "gin_main"
				want = false
			}
			ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
			defer cancel()
			s := Coordinator{Nodes: n, Control: c, Policy: DefaultPolicy()}
			err := s.converge(ctx, c.target, "upgrade", time.Millisecond)
			if (err == nil) != want {
				t.Fatalf("convergence: %v", err)
			}
			if c.maximum > 4 {
				t.Fatalf("unbounded fanout %d", c.maximum)
			}
			if kind == "success" && (c.starts != 12 || c.probes != 12) {
				t.Fatalf("starts/probes %d/%d", c.starts, c.probes)
			}
		})
	}
}
