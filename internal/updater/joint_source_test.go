package updater

import (
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/release"
)

// The native release fixture needs separate writable clones. Sharing one mounted
// Git checkout would let the reference reset race another node's fetch/build.
// These private origins are not production repository/PR/CI publication proof.
type jointSourceFixture struct {
	directory, origin string
	context           context.Context
	publication       *nativePublicationFixture
	current           *release.Manifest
}

var jointSourcePaths = []string{"Dockerfile", "Dockerfile.gin", "go.mod", "go.sum", "cmd", "internal", "migrations", "protocol", "static", "nginx", "updater/Dockerfile", "updater/Dockerfile.gin"}

func newJointSourceFixture(t *testing.T, ctx context.Context, source, root string, publication *nativePublicationFixture) *jointSourceFixture {
	t.Helper()
	fixture := &jointSourceFixture{directory: filepath.Join(root, "joint-source"), origin: filepath.Join(root, "joint-origin"), context: ctx, publication: publication}
	if err := os.Mkdir(fixture.directory, 0755); err != nil {
		t.Fatal(err)
	}
	for _, name := range jointSourcePaths {
		from := filepath.Join(source, filepath.FromSlash(name))
		if err := filepath.WalkDir(from, func(path string, entry fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			// Mutable caches/private dot files never become fixture artifacts.
			if strings.HasPrefix(entry.Name(), ".") || entry.Name() == "__pycache__" {
				if entry.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
			info, err := entry.Info()
			if err != nil {
				return err
			}
			if info.Mode()&os.ModeSymlink != 0 {
				return ErrState
			}
			relative, err := filepath.Rel(source, path)
			if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
				return ErrState
			}
			to := filepath.Join(fixture.directory, relative)
			if entry.IsDir() {
				return os.MkdirAll(to, 0755)
			}
			if !info.Mode().IsRegular() {
				return ErrState
			}
			if err := os.MkdirAll(filepath.Dir(to), 0755); err != nil {
				return err
			}
			input, err := os.Open(path)
			if err != nil {
				return err
			}
			defer input.Close()
			output, err := os.OpenFile(to, os.O_CREATE|os.O_EXCL|os.O_WRONLY, info.Mode().Perm())
			if err != nil {
				return err
			}
			_, err = io.Copy(output, input)
			return errors.Join(err, output.Close())
		}); err != nil {
			t.Fatal("private native source copy", name, err)
		}
	}
	fixture.git(t, "init", "-b", "gin_main")
	fixture.git(t, "config", "user.name", "Private Joint Release Test")
	fixture.git(t, "config", "user.email", "joint-release@example.invalid")
	fixture.git(t, "add", ".")
	fixture.git(t, "commit", "-m", "private joint baseline")
	fixture.git(t, "init", "--bare", fixture.origin)
	fixture.git(t, "remote", "add", "origin", fixture.origin)
	fixture.publish(t, "1.0.0")
	return fixture
}

func (f *jointSourceFixture) git(t *testing.T, arguments ...string) string {
	t.Helper()
	command := exec.CommandContext(f.context, "git", arguments...)
	command.Dir = f.directory
	raw, err := command.CombinedOutput()
	if err != nil {
		t.Fatal("private joint Git fixture", err, string(raw))
	}
	return strings.TrimSpace(string(raw))
}

func (f *jointSourceFixture) publish(t *testing.T, version string) *release.Manifest {
	t.Helper()
	target := f.git(t, "rev-parse", "HEAD")
	tree := f.git(t, "rev-parse", "HEAD^{tree}")
	args := []string{"commit-tree", tree, "-m", "private reference artifact " + version}
	if f.current != nil {
		args = append(args, "-p", f.current.Artifacts["main"].CommitSHA)
	}
	reference := f.git(t, args...)
	m := &release.Manifest{Format: "frontiercloud-release-manifest", Version: 1, ReleaseVersion: version, Protocol: 2, SchemaGeneration: 2, Artifacts: map[string]release.Artifact{}}
	for branch, commit := range map[string]string{"main": reference, "gin_main": target} {
		policy, _ := release.PolicyForBranch(branch)
		sourceArgs := []string{"commit-tree", tree, "-m", "private reviewed " + policy.Source + " " + version}
		if f.current != nil {
			sourceArgs = append(sourceArgs, "-p", f.current.Artifacts[branch].SourceSHA)
		}
		source := f.git(t, sourceArgs...)
		m.Artifacts[branch] = release.Artifact{Kind: "git-archive", CommitSHA: commit, SourceSHA: source, TreeSHA: tree}
		f.git(t, "update-ref", "refs/heads/"+branch, commit)
		f.git(t, "update-ref", "refs/heads/"+policy.Source, source)
	}
	f.git(t, "push", "origin", "gin_main", "main", "gin_dev", "dev")
	f.publication.mu.Lock()
	for branch, artifact := range m.Artifacts {
		policy, _ := release.PolicyForBranch(branch)
		f.publication.proofs[artifact.CommitSHA] = artifact
		f.publication.policies[artifact.CommitSHA] = policy
		f.publication.heads[branch] = artifact.CommitSHA
	}
	f.publication.mu.Unlock()
	f.current = release.CloneManifest(m)
	return m
}

func (f *jointSourceFixture) next(t *testing.T) *release.Manifest {
	t.Helper()
	if err := os.WriteFile(filepath.Join(f.directory, "static", "joint-release-fixture.txt"), []byte("private joint target\n"), 0644); err != nil {
		t.Fatal(err)
	}
	f.git(t, "add", "static/joint-release-fixture.txt")
	f.git(t, "commit", "-m", "private joint target")
	return f.publish(t, "2.0.0")
}

func (f *jointSourceFixture) clone(t *testing.T, destination, branch string) {
	t.Helper()
	if branch != "main" && branch != "gin_main" {
		t.Fatal("unknown private clone profile")
	}
	f.git(t, "clone", "--branch", branch, f.origin, destination)
}

func TestJointSourceFixtureDistinctProfilesPrivateClonesAndRollbackAncestry(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("Git CLI not installed")
	}
	publication := &nativePublicationFixture{proofs: map[string]release.Artifact{}, policies: map[string]release.Policy{}, heads: map[string]string{}, calls: map[string]int{}}
	// The native image build intentionally has no reference source. Test the
	// fixture copier with a synthetic complete tree instead of requiring Python
	// files to enter the native production archive/build context.
	source := t.TempDir()
	directories := map[string]bool{"cmd": true, "internal": true, "migrations": true, "protocol": true, "static": true, "nginx": true, "app": true, "tests": true}
	for _, name := range jointSourcePaths {
		path := filepath.Join(source, filepath.FromSlash(name))
		if directories[name] {
			path = filepath.Join(path, "fixture.txt")
		}
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte("private joint source fixture\n"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{".env", "data/fixture", "app/__pycache__/fixture.pyc", "app/.private-fixture"} {
		path := filepath.Join(source, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte("excluded private fixture\n"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	f := newJointSourceFixture(t, context.Background(), source, t.TempDir(), publication)
	old := release.CloneManifest(f.current)
	if old.Artifacts["main"].CommitSHA == old.Artifacts["gin_main"].CommitSHA || !old.Valid() {
		t.Fatal("joint source collapsed private profile artifacts")
	}
	for _, private := range []string{".env", "data", ".native-runtime", "app/__pycache__", "app/.private-fixture"} {
		if f.git(t, "ls-files", "--", private) != "" {
			t.Fatal("private/cache path entered joint artifact", private)
		}
	}
	for branch := range old.Artifacts {
		clone := filepath.Join(t.TempDir(), "source")
		f.clone(t, clone, branch)
		command := exec.Command("git", "rev-parse", "HEAD")
		command.Dir = clone
		raw, err := command.Output()
		if err != nil || strings.TrimSpace(string(raw)) != old.Artifacts[branch].CommitSHA {
			t.Fatal("private clone selected another artifact", branch, err)
		}
	}
	next := f.next(t)
	for branch, artifact := range next.Artifacts {
		if artifact.CommitSHA == old.Artifacts[branch].CommitSHA || publication.heads[branch] != artifact.CommitSHA || f.git(t, "rev-parse", artifact.CommitSHA+"^{tree}") != artifact.TreeSHA {
			t.Fatal("private target publication mismatch", branch)
		}
		f.git(t, "merge-base", "--is-ancestor", old.Artifacts[branch].CommitSHA, artifact.CommitSHA)
	}
}
