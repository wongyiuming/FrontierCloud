package updater

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/release"
)

func nativeManifest(version, target string) *release.Manifest {
	return &release.Manifest{Format: "frontiercloud-release-manifest", Version: 1, ReleaseVersion: version, Protocol: 2, SchemaGeneration: 2, Artifacts: map[string]release.Artifact{
		"main":     {Kind: "git-archive", CommitSHA: strings.Repeat("3", 40), SourceSHA: strings.Repeat("4", 40), TreeSHA: strings.Repeat("5", 40)},
		"gin_main": {Kind: "git-archive", CommitSHA: target, SourceSHA: strings.Repeat("6", 40), TreeSHA: strings.Repeat("7", 40)},
	}}
}

func TestConcreteManifestDistributionAndImageCompatibilityBoundaries(t *testing.T) {
	for _, fault := range []string{"", "distribution", "manifest-incompatible"} {
		t.Run(fault, func(t *testing.T) {
			x, f, status, target := executorFixture(t, fault)
			manifest := nativeManifest("2.0.0", target)
			a := manifest.Artifacts["gin_main"]
			x.ManifestVerifier = &manifestEvidence{proof: map[string]any{"available": true, "publishable": true, "branch": "gin_main", "source_branch": "gin_dev", "sha": a.CommitSHA, "ci_sha": a.SourceSHA, "tree_sha": a.TreeSHA, "status": "completed", "conclusion": "success"}}
			var committed bool
			_, err := x.Execute(context.Background(), Request{Target: target, Mode: "upgrade", Hold: true, Manifest: manifest}, status, func(c Checkpoint) error {
				if c.Current == target {
					committed = true
				}
				return nil
			})
			if (fault == "") != (err == nil) {
				t.Fatal("wrong execution result", err)
			}
			f.mu.Lock()
			defer f.mu.Unlock()
			web, _ := f.container("private-web")
			want := target
			if fault == "manifest-incompatible" {
				want = f.old
			}
			if f.images[web.Image].Config.Labels["frontiercloud.revision"] != want || committed != (fault != "manifest-incompatible") {
				t.Fatal("wrong local publication boundary", committed, err)
			}
			found := false
			for _, command := range f.commands {
				if len(command) > 1 && command[1] == "cluster-release" {
					t.Fatal("manifest reduced to one runtime's SHA")
				}
				if len(command) > 1 && command[1] == "cluster-release-manifest" {
					if len(command) != 4 || command[3] != "upgrade" {
						t.Fatal(command)
					}
					wire, decodeErr := protocol.Decode(command[2])
					parsed, parseErr := release.ParseManifest(wire)
					if decodeErr != nil || parseErr != nil {
						t.Fatal(decodeErr, parseErr)
					}
					id, _ := parsed.ID()
					expected, _ := manifest.ID()
					if id != expected {
						t.Fatal("distribution altered whole manifest")
					}
					found = true
				}
			}
			if found != (fault != "manifest-incompatible") {
				t.Fatal("distribution ran before compatibility validation")
			}
		})
	}
}

func TestManifestRecoveryRejectsDownlevelWebOrUpdaterImage(t *testing.T) {
	for _, component := range []string{"web", "updater"} {
		t.Run(component, func(t *testing.T) {
			x, f, status, _ := executorFixture(t, "")
			status.State, status.CurrentManifest = "success", nativeManifest("1.0.0", status.CurrentSHA)
			if err := x.Recover(context.Background(), status); err != nil {
				t.Fatal(err)
			}
			f.mu.Lock()
			c, _ := f.container("private-" + component)
			image := f.images[c.Image]
			delete(image.Config.Labels, "frontiercloud.release-manifest-version")
			f.images[c.Image] = image
			f.mu.Unlock()
			if err := x.Recover(context.Background(), status); !errors.Is(err, release.ErrManifest) {
				t.Fatal("downlevel image recovered manifest state", err)
			}
		})
	}
}

func TestJointReleaseUnchangedNativeArtifactCommitsWholeHistoryBeforeRemoteFailure(t *testing.T) {
	x, f, status, _ := executorFixture(t, "distribution")
	// The local stack is already on the fixture's reviewed production HEAD.
	status.CurrentSHA, status.RuntimeSHA, x.Runtime = status.TargetSHA, status.TargetSHA, status.TargetSHA
	f.mu.Lock()
	for ref, image := range f.images {
		image.Config.Labels["frontiercloud.revision"] = status.CurrentSHA
		f.images[ref] = image
	}
	f.mu.Unlock()
	manifest := nativeManifest("2.0.0", status.CurrentSHA)
	status.TargetSHA = status.CurrentSHA
	status.CurrentManifest = nativeManifest("1.0.0", status.CurrentSHA)
	a := manifest.Artifacts["gin_main"]
	x.ManifestVerifier = &manifestEvidence{proof: map[string]any{"available": true, "publishable": true, "branch": "gin_main", "source_branch": "gin_dev", "sha": a.CommitSHA, "ci_sha": a.SourceSHA, "tree_sha": a.TreeSHA, "status": "completed", "conclusion": "success"}}
	committed := false
	_, err := x.Execute(context.Background(), Request{Target: status.CurrentSHA, Mode: "upgrade", Hold: true, Manifest: manifest}, status, func(c Checkpoint) error {
		if c.Current == status.CurrentSHA && c.Phase == "local-committed" {
			committed = true
		}
		return nil
	})
	if err == nil || !committed {
		t.Fatal("joint local commit lost after remote failure", err, committed)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.removed) != 0 {
		t.Fatal("unchanged artifact replaced services", f.removed)
	}
}

func TestManifestQueueSelectsOnlyLocalPolicyDurableCloneAndIndependentHistory(t *testing.T) {
	d, control, flags := daemonFixture(t, &testExecutor{})
	initial := nativeManifest("1.0.0", testCurrent)
	d.status.CurrentManifest = initial
	if err := d.persistLocked(); err != nil {
		t.Fatal(err)
	}
	target := nativeManifest("2.0.0", testTarget)
	targetID, _ := target.ID()
	if _, err := d.Start(Request{Target: strings.Repeat("3", 40), Mode: "upgrade", Manifest: target}); !errors.Is(err, ErrState) {
		t.Fatal("Master selected reference artifact for native Follower", err)
	}
	out, err := d.Start(Request{Mode: "upgrade", Manifest: target})
	if err != nil || out["target_sha"] != testTarget || out["release_id"] != targetID {
		t.Fatal(out, err)
	}
	target.Artifacts["gin_main"] = release.Artifact{} // Caller mutation cannot corrupt accepted work.
	queued := <-d.queue
	if queued.Target != testTarget || !queued.Manifest.Valid() {
		t.Fatal("queue aliased caller manifest")
	}
	status := d.Status()
	status.TargetManifest.Artifacts["gin_main"] = release.Artifact{}
	if !d.Status().TargetManifest.Valid() {
		t.Fatal("status exposed mutable manifest map")
	}
	if err = d.checkpoint(Checkpoint{State: "running", Phase: "local-committed", Current: testTarget, Previous: testCurrent, SetPrevious: true}); err != nil {
		t.Fatal(err)
	}
	current := d.Status()
	if current.CurrentManifest.ReleaseVersion != "2.0.0" || current.PreviousManifest.ReleaseVersion != "1.0.0" || !current.valid() {
		t.Fatal(current)
	}
	if err = d.checkpoint(Checkpoint{State: "failed", Phase: "distribution-failed"}); err != nil {
		t.Fatal(err)
	}
	// A joint release can update only the reference artifact. Native SHA history
	// stays unchanged, but the previous whole release manifest must advance.
	sameArtifact := nativeManifest("3.0.0", testTarget)
	if _, err = d.Start(Request{Mode: "upgrade", Manifest: sameArtifact}); err != nil {
		t.Fatal(err)
	}
	<-d.queue
	if err = d.checkpoint(Checkpoint{State: "running", Phase: "local-committed", Current: testTarget}); err != nil {
		t.Fatal(err)
	}
	current = d.Status()
	if current.PreviousSHA != testCurrent || current.PreviousManifest.ReleaseVersion != "2.0.0" || current.CurrentManifest.ReleaseVersion != "3.0.0" || !current.valid() {
		t.Fatal("manifest history confused with SHA history", current)
	}
	if err = d.checkpoint(Checkpoint{State: "failed", Phase: "distribution-failed"}); err != nil {
		t.Fatal(err)
	}
	rollback := release.CloneManifest(current.PreviousManifest)
	if _, err = d.Start(Request{Mode: "rollback", Manifest: rollback}); err != nil {
		t.Fatal(err)
	}
	<-d.queue
	if err = d.checkpoint(Checkpoint{State: "running", Phase: "local-committed", Current: testTarget, Previous: "", SetPrevious: true}); err != nil {
		t.Fatal(err)
	}
	if d.Status().CurrentManifest.ReleaseVersion != "2.0.0" || d.Status().PreviousManifest != nil {
		t.Fatal("rollback invented previous joint release")
	}
	if err = d.Close(); err != nil {
		t.Fatal(err)
	}
	recovered, err := NewDaemon(context.Background(), control, flags, testCurrent, "gin_main", &testExecutor{})
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.Close()
	if recovered.Status().State != "failed" || recovered.Status().CurrentManifest.ReleaseVersion != "2.0.0" {
		t.Fatal("restart lost committed manifest", recovered.Status())
	}
}

type manifestEvidence struct {
	proof  map[string]any
	err    error
	target string
}

func (e *manifestEvidence) Artifact(ctx context.Context, target string) (map[string]any, error) {
	e.target = target
	return e.proof, e.err
}

func TestExecutorManifestRequiresIndependentExactSelectedArtifactEvidence(t *testing.T) {
	m := nativeManifest("2.0.0", testTarget)
	a := m.Artifacts["gin_main"]
	evidence := &manifestEvidence{proof: map[string]any{"available": true, "publishable": true, "branch": "gin_main", "source_branch": "gin_dev", "sha": a.CommitSHA, "ci_sha": a.SourceSHA, "tree_sha": a.TreeSHA, "status": "completed", "conclusion": "success"}}
	executor := &DockerExecutor{Source: Source{Branch: "gin_main"}, ManifestVerifier: evidence}
	request := Request{Target: testTarget, Mode: "upgrade", Manifest: m}
	before := Status{ReleaseBranch: "gin_main"}
	if err := executor.verifyManifest(context.Background(), request, before); err != nil || evidence.target != testTarget {
		t.Fatal(err, evidence.target)
	}
	evidence.proof["publishable"] = false
	if _, err := executor.Execute(context.Background(), request, before, func(Checkpoint) error { t.Fatal("unverified manifest reached progress mutation"); return nil }); !errors.Is(err, release.ErrManifest) {
		t.Fatal("failed CI reached Docker/source mutation", err)
	}
	evidence.proof["publishable"] = true
	evidence.proof["tree_sha"] = strings.Repeat("0", 40)
	if err := executor.verifyManifest(context.Background(), request, before); !errors.Is(err, release.ErrManifest) {
		t.Fatal("other tree reused", err)
	}
}
