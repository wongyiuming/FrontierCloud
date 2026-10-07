package release

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type publishedFixture struct {
	manifest Manifest
	calls    int
}

func (s *publishedFixture) Latest(context.Context) (Manifest, error) {
	s.calls++
	return *CloneManifest(&s.manifest), nil
}

type jointProof struct {
	value                    map[string]any
	artifactCalls, headCalls int
	before                   func()
}

func (p *jointProof) Artifact(_ context.Context, target string) (map[string]any, error) {
	p.artifactCalls++
	if p.before != nil {
		p.before()
	}
	return copyValue(p.value), nil
}
func (p *jointProof) Status(context.Context, bool) (map[string]any, error) {
	p.headCalls++
	return copyValue(p.value), nil
}

type jointAgent struct {
	local  map[string]any
	starts []map[string]any
	unack  bool
	old    bool
}

func (a *jointAgent) Request(_ context.Context, request map[string]any) (map[string]any, error) {
	if request["action"] == "status" {
		out := map[string]any{"ok": true, "status": copyValue(a.local)}
		if !a.old {
			out["capabilities"] = []string{ManifestCapability}
		}
		return out, nil
	}
	a.starts = append(a.starts, copyValue(request))
	m, err := ManifestFromValue(request["release_manifest"])
	if err != nil {
		return nil, err
	}
	id, _ := m.ID()
	if a.unack {
		id = "wrong"
	}
	return map[string]any{"ok": true, "accepted": true, "release_id": id}, nil
}

func TestPublishedManifestStartBothProfilesProofFreshAuthorityAndWholeHistory(t *testing.T) {
	m := sharedManifest(t)
	for _, kind := range []string{"upgrade", "rollback", "joint-same-sha", "converged", "old-peer", "old-local", "failed-ci", "wrong-tree", "role-change", "membership-change", "busy", "sha-history-only", "unacknowledged"} {
		t.Run(kind, func(t *testing.T) {
			nodes := &fixtureNodes{identity: store.NodeIdentity{ID: strings.Repeat("1", 32), Role: "Master"}, relations: []store.Relationship{{ID: strings.Repeat("2", 32), PeerID: strings.Repeat("3", 32), Endpoint: "https://peer.test", PublicKey: "pin", Direction: "downstream", State: "active", Protocol: 2}}}
			agent := &jointAgent{local: map[string]any{"release_branch": "gin_main", "current_sha": strings.Repeat("0", 40), "state": "success", "previous_manifest": m, "previous_sha": strings.Repeat("9", 40)}}
			source := &publishedFixture{manifest: m}
			caller := &manifestCaller{manifest: m, modes: map[string]string{nodes.relations[0].ID: "main"}}
			proofs := map[string]ArtifactEvidence{}
			for _, branch := range []string{"main", "gin_main"} {
				policy, _ := PolicyForBranch(branch)
				a := m.Artifacts[branch]
				proofs[branch] = &jointProof{value: map[string]any{"available": true, "publishable": true, "branch": branch, "source_branch": policy.Source, "sha": a.CommitSHA, "ci_sha": a.SourceSHA, "tree_sha": a.TreeSHA, "status": "completed", "conclusion": "success"}}
			}
			mode, success := "upgrade", true
			switch kind {
			case "rollback":
				mode = "rollback"
			case "joint-same-sha":
				old := *CloneManifest(&m)
				old.ReleaseVersion = "1.0.0"
				agent.local["current_sha"], agent.local["current_manifest"] = m.Artifacts["gin_main"].CommitSHA, old
			case "converged":
				agent.local["current_sha"], agent.local["current_manifest"] = m.Artifacts["gin_main"].CommitSHA, m
				success = false
			case "old-peer":
				caller.old = true
				success = false
			case "old-local":
				agent.old = true
				success = false
			case "failed-ci":
				proofs["main"].(*jointProof).value["publishable"] = false
				success = false
			case "wrong-tree":
				proofs["main"].(*jointProof).value["tree_sha"] = strings.Repeat("9", 40)
				success = false
			case "role-change":
				proofs["main"].(*jointProof).before = func() { nodes.identity.Role = "Standalone" }
				success = false
			case "membership-change":
				proofs["main"].(*jointProof).before = func() { nodes.relations[0].PublicKey = "new pin" }
				success = false
			case "busy":
				agent.local["state"] = "running"
				success = false
			case "sha-history-only":
				mode = "rollback"
				delete(agent.local, "previous_manifest")
				success = false
			case "unacknowledged":
				agent.unack = true
				success = false
			}
			coordinator := Coordinator{Agent: agent, Nodes: nodes, Control: caller, Policy: Policy{"gin_main", "gin_dev"}, ManifestSource: source, ManifestEvidence: proofs}
			out, err := coordinator.Start(context.Background(), mode)
			if (err == nil) != success {
				t.Fatal(kind, out, err)
			}
			if success {
				if len(agent.starts) != 1 || agent.starts[0]["hold_maintenance"] != true {
					t.Fatal(agent.starts)
				}
				if _, chosen := agent.starts[0]["target_sha"]; chosen {
					t.Fatal("Master chose artifact SHA")
				}
				selected, err := ManifestFromValue(agent.starts[0]["release_manifest"])
				id, _ := selected.ID()
				expected, _ := m.ID()
				if err != nil || id != expected {
					t.Fatal("whole manifest altered", err)
				}
				for _, proof := range proofs {
					if proof.(*jointProof).artifactCalls != 1 {
						t.Fatal("missing independent profile proof")
					}
				}
			}
			if !success && kind != "unacknowledged" && len(agent.starts) != 0 {
				t.Fatal("unverified intent reached queue")
			}
			if mode == "rollback" && source.calls != 0 {
				t.Fatal("rollback replaced durable history with latest source")
			}
		})
	}
}

func TestFileManifestSourceBoundedImmutableReadNoSymlinkOrInvalidShape(t *testing.T) {
	m := sharedManifest(t)
	wire, _ := m.Wire()
	path := filepath.Join(t.TempDir(), "release.json")
	if err := os.WriteFile(path, wire, 0600); err != nil {
		t.Fatal(err)
	}
	source := FileManifestSource{Path: path}
	if _, err := source.Latest(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, raw := range [][]byte{[]byte(`{"release_version":"1.0.0"}`), []byte(strings.Repeat(" ", MaxManifestBytes+1))} {
		if err := os.WriteFile(path, raw, 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := source.Latest(context.Background()); err == nil {
			t.Fatal("invalid publication metadata read")
		}
	}
	if _, err := (FileManifestSource{Path: "relative.json"}).Latest(context.Background()); err == nil {
		t.Fatal("relative publication source accepted")
	}
	link := filepath.Join(filepath.Dir(path), "link.json")
	if err := os.Symlink(path, link); err == nil {
		if _, err := (FileManifestSource{Path: link}).Latest(context.Background()); err == nil {
			t.Fatal("symlink publication source accepted")
		}
	}
}
