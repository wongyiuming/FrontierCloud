package release

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type manifestCaller struct {
	mu                                                 sync.Mutex
	manifest                                           Manifest
	modes                                              map[string]string
	starts                                             int
	failed, old, wrongArtifact, wrongManifest, unack   bool
	before                                             func()
	unavailable, outage, dropCapability, changeProfile bool
	probes                                             map[string]bool
}

func (c *manifestCaller) Call(ctx context.Context, r store.Relationship, route string, value any) (map[string]any, error) {
	if c.before != nil {
		c.before()
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	branch := c.modes[r.ID]
	artifact := c.manifest.Artifacts[branch]
	id, _ := c.manifest.ID()
	whole, _ := c.manifest.Wire()
	m, _ := ManifestFromValue(jsonValue(whole))
	if route == "/internal/v1/cluster-update/start" {
		body := value.(map[string]any)
		if _, present := body["target_sha"]; present {
			return nil, errors.New("Master dictated a local target")
		}
		request, err := ManifestFromValue(body["release_manifest"])
		if err != nil {
			return nil, err
		}
		requestID, _ := request.ID()
		if requestID != id {
			return nil, ErrManifest
		}
		c.starts++
		if c.unack {
			return map[string]any{"accepted": true, "release_id": "wrong"}, nil
		}
		return map[string]any{"accepted": true, "release_id": id}, nil
	}
	if c.starts > 0 {
		if c.outage || c.unavailable && !c.probes[r.ID] {
			if c.probes == nil {
				c.probes = map[string]bool{}
			}
			c.probes[r.ID] = true
			return nil, errors.New("node control HTTP 503")
		}
		if c.changeProfile {
			branch = map[string]string{"main": "gin_main", "gin_main": "main"}[branch]
		}
	}
	state := "success"
	if c.failed {
		state = "failed"
	}
	sha := artifact.CommitSHA
	if c.wrongArtifact {
		sha = c.manifest.Artifacts["gin_main"].CommitSHA
	}
	current := m
	if c.wrongManifest {
		current.ReleaseVersion = "other"
	}
	out := map[string]any{"status": map[string]any{"release_branch": branch, "state": state, "current_sha": sha, "current_manifest": current, "target_manifest": m}, "capabilities": []string{ManifestCapability}}
	if c.old || c.dropCapability && c.starts > 0 {
		delete(out, "capabilities")
	}
	return out, nil
}
func jsonValue(raw []byte) any { var value any; _ = json.Unmarshal(raw, &value); return value }

func TestManifestConvergenceDistinctPrivateArtifactsCapabilitiesAndWholeDigest(t *testing.T) {
	m := sharedManifest(t)
	for _, kind := range []string{"success", "handoff-socket", "handoff-outage", "capability-drop", "profile-change", "old-peer", "failed", "wrong-artifact", "wrong-manifest", "unacknowledged", "reset"} {
		t.Run(kind, func(t *testing.T) {
			nodes := &fixtureNodes{identity: store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Master"}}
			caller := &manifestCaller{manifest: m, modes: map[string]string{}}
			for i := 0; i < 9; i++ {
				r := store.Relationship{ID: fmt.Sprintf("%032x", i+1), PeerID: fmt.Sprintf("%032x", 100+i), Endpoint: "https://peer.test", PublicKey: "pin", Direction: "downstream", State: "active", Protocol: 2}
				nodes.relations = append(nodes.relations, r)
				branch := "main"
				if i%2 == 0 {
					branch = "gin_main"
				}
				caller.modes[r.ID] = branch
			}
			switch kind {
			case "handoff-socket":
				caller.unavailable = true
			case "handoff-outage":
				caller.outage = true
			case "capability-drop":
				caller.dropCapability = true
			case "profile-change":
				caller.changeProfile = true
			case "old-peer":
				caller.old = true
			case "failed":
				caller.failed = true
			case "wrong-artifact":
				caller.wrongArtifact = true
			case "wrong-manifest":
				caller.wrongManifest = true
			case "unacknowledged":
				caller.unack = true
			case "reset":
				caller.before = func() { nodes.mu.Lock(); nodes.identity.Role = "Standalone"; nodes.mu.Unlock() }
			}
			deadline := 2 * time.Second
			if kind == "wrong-artifact" || kind == "wrong-manifest" || kind == "handoff-outage" {
				deadline = 200 * time.Millisecond
			}
			ctx, cancel := context.WithTimeout(context.Background(), deadline)
			defer cancel()
			coordinator := Coordinator{Nodes: nodes, Control: caller, Policy: Policy{"gin_main", "gin_dev"}}
			err := coordinator.convergeManifest(ctx, m, "upgrade", time.Millisecond)
			success := kind == "success" || kind == "handoff-socket"
			if (err == nil) != success {
				t.Fatal(kind, err)
			}
			if success && caller.starts != 9 {
				t.Fatal("missing peers", caller.starts)
			}
			if kind == "handoff-socket" && len(caller.probes) != 9 {
				t.Fatal("socket gap did not exercise every private profile")
			}
			if (kind == "old-peer" || kind == "reset") && caller.starts != 0 {
				t.Fatal("dispatched before complete capability/authority preflight")
			}
		})
	}
}
