package release

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/migrations"
)

const MaxManifestBytes = 8192
const ManifestCapability = "release-manifest-v1"

var releaseVersion = regexp.MustCompile(`^[0-9][a-zA-Z0-9._-]{0,63}$`)
var ErrManifest = errors.New("invalid or incompatible release manifest")

type ArtifactEvidence interface {
	Artifact(context.Context, string) (map[string]any, error)
}

// Profiles identify locally configured publication policies, not languages or
// databases inferred from a peer. Every peer receives the same whole manifest.
type Artifact struct {
	Kind      string `json:"kind"`
	CommitSHA string `json:"commit_sha"`
	SourceSHA string `json:"source_sha"`
	TreeSHA   string `json:"tree_sha"`
}
type Manifest struct {
	Format           string              `json:"format"`
	Version          int                 `json:"version"`
	ReleaseVersion   string              `json:"release_version"`
	Protocol         int                 `json:"protocol"`
	SchemaGeneration int                 `json:"schema_generation"`
	Artifacts        map[string]Artifact `json:"artifacts"`
}

func (m Manifest) Valid() bool {
	if m.Format != "frontiercloud-release-manifest" || m.Version != 1 || !releaseVersion.MatchString(m.ReleaseVersion) || m.Protocol != protocol.Version || m.SchemaGeneration != migrations.Generation || len(m.Artifacts) != 2 {
		return false
	}
	for _, profile := range []string{"main", "gin_main"} {
		a, ok := m.Artifacts[profile]
		if !ok || a.Kind != "git-archive" || !ValidSHA(a.CommitSHA) || !ValidSHA(a.SourceSHA) || !ValidSHA(a.TreeSHA) {
			return false
		}
	}
	return true
}

func ParseManifest(raw []byte) (Manifest, error) {
	var result Manifest
	value, err := protocol.ParseStrictJSON(raw, MaxManifestBytes)
	if err != nil {
		return result, ErrManifest
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&result) != nil || !result.Valid() {
		return Manifest{}, ErrManifest
	}
	// Reject null/missing required fields and noncanonical integer forms, not
	// just unknown fields. The digest binds all declared fields without loss.
	expected, err := result.Wire()
	if err != nil {
		return Manifest{}, ErrManifest
	}
	actual, err := protocol.Canonical(value)
	if err != nil || !bytes.Equal(expected, actual) {
		return Manifest{}, ErrManifest
	}
	return result, nil
}
func (m Manifest) Wire() ([]byte, error) {
	if !m.Valid() {
		return nil, ErrManifest
	}
	raw, err := json.Marshal(m)
	if err != nil {
		return nil, ErrManifest
	}
	value, err := protocol.ParseStrictJSON(raw, MaxManifestBytes)
	if err != nil {
		return nil, ErrManifest
	}
	return protocol.Canonical(value)
}
func (m Manifest) ID() (string, error) {
	raw, err := m.Wire()
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}
func (m Manifest) Select(policy Policy) (Artifact, error) {
	if !m.Valid() || !policy.Valid() {
		return Artifact{}, ErrManifest
	}
	return m.Artifacts[policy.Branch], nil
}

// A well-formed manifest is not publication authority. Current exact reviewed
// tree/CI evidence must independently validate the locally selected artifact.
func (m Manifest) CheckEvidence(policy Policy, evidence map[string]any) error {
	a, err := m.Select(policy)
	if err != nil {
		return err
	}
	if evidence["publishable"] != true || evidence["available"] != true || evidence["branch"] != policy.Branch || evidence["source_branch"] != policy.Source || evidence["sha"] != a.CommitSHA || evidence["ci_sha"] != a.SourceSHA || evidence["tree_sha"] != a.TreeSHA || evidence["status"] != "completed" || evidence["conclusion"] != "success" {
		return ErrManifest
	}
	return nil
}

func PolicyForBranch(branch string) (Policy, error) {
	p := Policy{Branch: branch}
	switch branch {
	case "main":
		p.Source = "dev"
	case "gin_main":
		p.Source = "gin_dev"
	default:
		return Policy{}, ErrManifest
	}
	return p, nil
}

// A parsed copy avoids mutable caller maps escaping into queued/durable state.
func CloneManifest(value *Manifest) *Manifest {
	if value == nil {
		return nil
	}
	raw, err := value.Wire()
	if err != nil {
		return nil
	}
	copy, err := ParseManifest(raw)
	if err != nil {
		return nil
	}
	return &copy
}
