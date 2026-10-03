package release

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func sharedManifest(t *testing.T) Manifest {
	t.Helper()
	raw, err := os.ReadFile("../../protocol/v2/vectors/release-manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var vectors struct {
		Cases []struct {
			Name     string          `json:"name"`
			Manifest json.RawMessage `json:"manifest"`
			Raw      string          `json:"raw"`
			Valid    bool            `json:"valid"`
		} `json:"cases"`
	}
	if err = json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	var first Manifest
	for _, v := range vectors.Cases {
		t.Run(v.Name, func(t *testing.T) {
			raw := v.Manifest
			if v.Raw != "" {
				raw = []byte(v.Raw)
			}
			m, err := ParseManifest(raw)
			if (err == nil) != v.Valid {
				t.Fatal("valid", v.Valid, err)
			}
			if v.Valid {
				first = m
			}
		})
	}
	return first
}

func TestSharedManifestBoundedProtocolAndDistinctPrivatePolicies(t *testing.T) {
	m := sharedManifest(t)
	id, err := m.ID()
	if err != nil || id != "6db944731759d32042875701c66049ff9df0bfacd7f10eb8920ef2b7c9c25e70" {
		t.Fatal(id, err)
	}
	reference, err := m.Select(DefaultPolicy())
	if err != nil {
		t.Fatal(err)
	}
	native, err := m.Select(Policy{"gin_main", "gin_dev"})
	if err != nil {
		t.Fatal(err)
	}
	if reference.CommitSHA != strings.Repeat("a", 40) || native.CommitSHA != strings.Repeat("d", 40) || reference.CommitSHA == native.CommitSHA {
		t.Fatal("whole manifest assumed one common commit")
	}
	if _, err = m.Select(Policy{"gin_main", "dev"}); err == nil {
		t.Fatal("unconfigured profile accepted")
	}
	if _, err = ParseManifest([]byte(strings.Repeat(" ", MaxManifestBytes+1))); err == nil {
		t.Fatal("oversize manifest accepted")
	}
	wire, err := m.Wire()
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseManifest(wire)
	if err != nil {
		t.Fatal(err)
	}
	again, _ := parsed.ID()
	if again != id {
		t.Fatal("canonical identity changed")
	}
	// ReleaseVersion is a label, never a replacement for protocol/schema gates.
	parsed.ReleaseVersion = "3.9.8-independent-label"
	if !parsed.Valid() {
		t.Fatal("version label inferred protocol compatibility")
	}
}

func TestManifestIsNotAuthorityWithoutExactIndependentCIEvidence(t *testing.T) {
	m := sharedManifest(t)
	p := Policy{"gin_main", "gin_dev"}
	artifact, _ := m.Select(p)
	evidence := map[string]any{"publishable": true, "available": true, "branch": p.Branch, "source_branch": p.Source, "sha": artifact.CommitSHA, "ci_sha": artifact.SourceSHA, "tree_sha": artifact.TreeSHA, "status": "completed", "conclusion": "success"}
	if err := m.CheckEvidence(p, evidence); err != nil {
		t.Fatal(err)
	}
	for key, value := range map[string]any{"publishable": false, "available": false, "branch": "main", "source_branch": "dev", "sha": strings.Repeat("a", 40), "ci_sha": strings.Repeat("b", 40), "tree_sha": strings.Repeat("c", 40), "status": "running", "conclusion": "failure"} {
		old := evidence[key]
		evidence[key] = value
		if err := m.CheckEvidence(p, evidence); err == nil {
			t.Fatal("unverified artifact accepted", key)
		}
		evidence[key] = old
	}
	if err := m.CheckEvidence(DefaultPolicy(), evidence); err == nil {
		t.Fatal("native proof used for reference artifact")
	}
}
