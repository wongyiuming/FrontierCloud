package deployment

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// Executes the actual operator script against an isolated Git repository and a
// capture-only Engine CLI. Real image builds remain a separate acceptance gate.
func TestBootstrapArchivesExactCommitAndRejectsMutableSelections(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("Linux operator bootstrap requires bash/git/tar")
	}
	for _, name := range []string{"bash", "git", "tar"} {
		if _, err := exec.LookPath(name); err != nil {
			t.Skip("operator tool unavailable: " + name)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	root := t.TempDir()
	write := func(name, data string, mode os.FileMode) {
		t.Helper()
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(data), mode); err != nil {
			t.Fatal(err)
		}
	}
	fixture := map[string]string{
		"Dockerfile.gin": "FROM committed-native\n", "updater/Dockerfile.gin": "FROM committed-updater\n",
		"nginx/Dockerfile": "FROM committed-edge\n", "go.mod": "module fixture\n", "go.sum": "committed-sum\n",
		"cmd/entry.txt": "committed\n", "internal/core.txt": "committed\n", "migrations/schema.txt": "committed\n",
		"protocol/contract.txt": "committed\n", "static/index.txt": "committed\n",
		".env": "private-tracked-secret\n", "data/key": "private-tracked-key\n",
		"app/main.py": "excluded reference runtime\n", "updater/server.py": "excluded reference updater\n",
	}
	for name, data := range fixture {
		write(name, data, 0600)
	}
	git := func(args ...string) string {
		t.Helper()
		cmd := exec.CommandContext(ctx, "git", args...)
		cmd.Dir = root
		cmd.Env = append(os.Environ(), "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null")
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("fixture Git failed: %v: %s", err, out)
		}
		return strings.TrimSpace(string(out))
	}
	git("init", "-q")
	git("add", ".")
	git("-c", "user.name=Fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "immutable fixture")
	revision := git("rev-parse", "HEAD")
	script, err := os.ReadFile(filepath.Join("..", "..", "scripts", "build-native-images.sh"))
	if os.IsNotExist(err) {
		t.Skip("operator script is source-checkout-only, not shipped in the native runtime build")
	}
	if err != nil {
		t.Fatal(err)
	}
	write("scripts/build-native-images.sh", string(script), 0700)
	write("cmd/entry.txt", "mutable-source-must-not-enter\n", 0600)
	write("cmd/untracked.txt", "untracked-must-not-enter\n", 0600)
	write("fake-bin/docker", `#!/usr/bin/env bash
set -euo pipefail
test "$1" = --host
test "$2" = unix:///var/run/docker.sock
test "$3" = build
printf '%s\n' "$*" >> "$BOOTSTRAP_CAPTURE/args"
dest=$(mktemp -d "$BOOTSTRAP_CAPTURE/archive-XXXXXXXX")
tar xf - -C "$dest"
test "$(cat "$dest/cmd/entry.txt")" = committed
for excluded in .env data .git app updater/server.py cmd/untracked.txt; do
  test ! -e "$dest/$excluded"
done
`, 0700)
	capture := filepath.Join(root, "capture")
	if err := os.Mkdir(capture, 0700); err != nil {
		t.Fatal(err)
	}
	run := func(selection string) ([]byte, error) {
		cmd := exec.CommandContext(ctx, "bash", "scripts/build-native-images.sh", selection)
		cmd.Dir = root
		cmd.Env = append(os.Environ(), "PATH="+filepath.Join(root, "fake-bin")+":"+os.Getenv("PATH"), "BOOTSTRAP_CAPTURE="+capture)
		return cmd.CombinedOutput()
	}
	for _, invalid := range []string{"HEAD", "gin_dev", revision[:12], strings.Repeat("f", 40)} {
		if out, err := run(invalid); err == nil {
			t.Fatalf("mutable/missing revision accepted: %s: %s", invalid, out)
		}
	}
	if _, err := os.Stat(filepath.Join(capture, "args")); !os.IsNotExist(err) {
		t.Fatal("invalid source selection reached Engine")
	}
	if out, err := run(revision); err != nil {
		t.Fatalf("immutable bootstrap failed: %v: %s", err, out)
	}
	args, err := os.ReadFile(filepath.Join(capture, "args"))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(args)), "\n")
	if len(lines) != 3 {
		t.Fatal("bootstrap did not build exactly three images")
	}
	for index, component := range []string{"web", "updater", "nginx"} {
		if !strings.Contains(lines[index], "REVISION="+revision) || !strings.Contains(lines[index], "frontiercloud-go-"+component+":"+revision) || !strings.HasSuffix(lines[index], " -") {
			t.Fatal("immutable component context/label mismatch", component)
		}
	}
	if !strings.Contains(lines[2], "FRONTIERCLOUD_RUNTIME=go") {
		t.Fatal("edge lost explicit native runtime label")
	}
}
