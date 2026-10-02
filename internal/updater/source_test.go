package updater

import (
	"archive/tar"
	"context"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func gitFixture(t *testing.T) (Source, string, string) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git unavailable")
	}
	root := t.TempDir()
	repo := filepath.Join(root, "source")
	remote := filepath.Join(root, "remote.git")
	run := func(directory string, args ...string) string {
		t.Helper()
		cmd := exec.Command("git", args...)
		cmd.Dir = directory
		raw, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git fixture %v: %v %s", args, err, raw)
		}
		return strings.TrimSpace(string(raw))
	}
	run(root, "init", "--bare", remote)
	run(root, "init", "-b", "gin_main", repo)
	run(repo, "config", "user.name", "Native Test")
	run(repo, "config", "user.email", "native-test@example.invalid")
	for _, name := range archivePaths {
		path := filepath.Join(repo, filepath.FromSlash(name))
		if strings.Contains(name, ".") {
			os.MkdirAll(filepath.Dir(path), 0750)
			if err := os.WriteFile(path, []byte("tracked "+name), 0600); err != nil {
				t.Fatal(err)
			}
		} else {
			os.MkdirAll(path, 0750)
			if err := os.WriteFile(filepath.Join(path, "fixture.txt"), []byte("tracked "+name), 0600); err != nil {
				t.Fatal(err)
			}
		}
	}
	run(repo, "add", ".")
	run(repo, "commit", "-m", "first")
	old := run(repo, "rev-parse", "HEAD")
	os.WriteFile(filepath.Join(repo, "go.mod"), []byte("target go.mod"), 0600)
	run(repo, "add", "go.mod")
	run(repo, "commit", "-m", "second")
	target := run(repo, "rev-parse", "HEAD")
	run(repo, "remote", "add", "origin", remote)
	run(repo, "push", "origin", "gin_main")
	return Source{Directory: repo, Branch: "gin_main"}, old, target
}
func TestSourceValidatesHeadAndArchivesWithoutTouchingWorktree(t *testing.T) {
	s, old, target := gitFixture(t)
	ctx := context.Background()
	if err := s.Validate(ctx, target, "upgrade"); err != nil {
		t.Fatal(err)
	}
	if err := s.Validate(ctx, old, "upgrade"); err == nil {
		t.Fatal("stale HEAD accepted")
	}
	if err := s.Validate(ctx, old, "rollback"); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(s.Directory, ".env"), []byte("private credential"), 0600)
	os.Mkdir(filepath.Join(s.Directory, "data"), 0700)
	os.WriteFile(filepath.Join(s.Directory, "data", "private"), []byte("private media"), 0600)
	if err := s.Validate(ctx, target, "upgrade"); err != nil {
		t.Fatal("untracked data blocked immutable release", err)
	}
	archive, err := s.Archive(ctx, old)
	if err != nil {
		t.Fatal(err)
	}
	reader := tar.NewReader(archive)
	files := 0
	for {
		h, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if h.Typeflag != tar.TypeReg {
			continue
		}
		files++
		raw, err := io.ReadAll(reader)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(raw), "private") || strings.HasPrefix(h.Name, "data/") || strings.HasPrefix(h.Name, ".git/") || h.Name == ".env" {
			t.Fatal("working-tree bytes leaked")
		}
		if h.Name == "go.mod" && string(raw) != "tracked go.mod" {
			t.Fatal("archive used mutable HEAD")
		}
	}
	if _, err = io.Copy(io.Discard, archive); err != nil {
		t.Fatal(err)
	}
	if err = archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err = archive.Close(); err != nil {
		t.Fatal("close not idempotent", err)
	}
	if files != len(archivePaths) {
		t.Fatalf("files %d", files)
	}
	raw, _ := os.ReadFile(filepath.Join(s.Directory, "go.mod"))
	if string(raw) != "target go.mod" {
		t.Fatal("checkout was mutated")
	}
	raw, _ = os.ReadFile(filepath.Join(s.Directory, ".env"))
	if string(raw) != "private credential" {
		t.Fatal("untracked file changed")
	}
	os.WriteFile(filepath.Join(s.Directory, "go.mod"), []byte("dirty work"), 0600)
	if err = s.Validate(ctx, target, "upgrade"); err == nil {
		t.Fatal("dirty tracked source admitted")
	}
	raw, _ = os.ReadFile(filepath.Join(s.Directory, "go.mod"))
	if string(raw) != "dirty work" {
		t.Fatal("dirty source overwritten")
	}
}
