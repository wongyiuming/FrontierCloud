package updater

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// This opt-in test only creates a fresh private project/network/volume and
// uniquely committed fixture images. It never selects deployed service names.
func TestRealDockerEngineArchiveHelpersReplacementAndHealth(t *testing.T) {
	socket := os.Getenv("FRONTIERCLOUD_TEST_DOCKER_SOCKET")
	if socket == "" {
		t.Skip("disposable Docker Engine test not selected")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	e, err := NewEngine(ctx, socket)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	var random [12]byte
	rand.Read(random[:])
	prefix := "fc-native-engine-" + hex.EncodeToString(random[:])
	e.Project = prefix
	var networkID, volumeName string
	var imageRefs []string
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		defer cancel()
		for _, name := range []string{prefix + "-helper", prefix + "-web"} {
			if c, err := e.Inspect(cleanup, name); err == nil && (c.label("com.docker.compose.project") == prefix || c.label("frontiercloud.project") == prefix) {
				e.Stop(cleanup, c.ID)
				e.Remove(cleanup, c.ID)
			}
		}
		for _, ref := range imageRefs {
			e.call(cleanup, "DELETE", "/images/"+ref+"?force=false&noprune=true", nil, nil)
		}
		if volumeName != "" {
			e.call(cleanup, "DELETE", "/volumes/"+volumeName, nil, nil)
		}
		if networkID != "" {
			e.call(cleanup, "DELETE", "/networks/"+networkID, nil, nil)
		}
	}()
	var network struct {
		ID string `json:"Id"`
	}
	if err = e.call(ctx, "POST", "/networks/create", map[string]any{"Name": prefix + "-net", "Internal": true}, &network); err != nil {
		t.Fatal(err)
	}
	networkID = network.ID
	var volume struct {
		Name string `json:"Name"`
	}
	if err = e.call(ctx, "POST", "/volumes/create", map[string]any{"Name": prefix + "-volume"}, &volume); err != nil {
		t.Fatal(err)
	}
	volumeName = volume.Name
	s, _, _ := gitFixture(t)
	if err = os.WriteFile(filepath.Join(s.Directory, "Dockerfile.gin"), []byte("FROM nginx:1.30.4-alpine\nRUN printf 'native-test\\n' > /fixture-revision\n"), 0600); err != nil {
		t.Fatal(err)
	}
	git := func(args ...string) string {
		cmd := exec.Command("git", args...)
		cmd.Dir = s.Directory
		raw, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatal(err, string(raw))
		}
		return strings.TrimSpace(string(raw))
	}
	git("add", "Dockerfile.gin")
	git("commit", "-m", "private Docker fixture "+prefix)
	old := git("rev-parse", "HEAD")
	oldImage, err := e.Build(ctx, s, old, "web", "Dockerfile.gin")
	if err != nil {
		t.Fatal(err)
	}
	imageRefs = append(imageRefs, oldImage)
	labels := map[string]any{"com.docker.compose.project": prefix, "com.docker.compose.service": "web"}
	body := map[string]any{"Image": oldImage, "Entrypoint": []string{"/bin/sh"}, "Cmd": []string{"-c", "sleep 120"}, "StopSignal": "SIGKILL", "User": "10001:10001", "Labels": labels, "Env": []string{"EXACT_ENV=preserved"}, "Healthcheck": map[string]any{"Test": []string{"CMD", "/bin/sh", "-c", "test -r /fixture-revision"}, "Interval": int64(time.Second), "Timeout": int64(time.Second), "Retries": 3}, "HostConfig": map[string]any{"ReadonlyRootfs": true, "CapDrop": []string{"ALL"}, "Binds": []string{volume.Name + ":/private:rw"}, "Memory": 64 << 20, "NetworkMode": prefix + "-net", "RestartPolicy": map[string]any{"Name": "no"}}, "NetworkingConfig": map[string]any{"EndpointsConfig": map[string]any{prefix + "-net": map[string]any{"Aliases": []string{"web"}}}}}
	id, err := e.Create(ctx, prefix+"-web", body)
	if err != nil {
		t.Fatal(err)
	}
	if err = e.Start(ctx, id); err != nil {
		t.Fatal(err)
	}
	if err = e.Healthy(ctx, id); err != nil {
		t.Fatal(err)
	}
	snapshot, err := e.Service(ctx, prefix, "web")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.Helper(ctx, snapshot, oldImage, prefix+"-helper", []string{"-c", "test \"$EXACT_ENV\" = preserved && test -d /private"}, false); err != nil {
		t.Fatal(err)
	}
	if err = e.Exec(ctx, id, []string{"/bin/sh", "-c", "exit 7"}); err == nil {
		t.Fatal("failed exec reported success")
	}
	if _, err = e.Inspect(ctx, prefix+"-helper"); !errors.Is(err, ErrNotFound) {
		t.Fatal("completed helper retained", err)
	}
	if err = os.WriteFile(filepath.Join(s.Directory, "Dockerfile.gin"), []byte("FROM nginx:1.30.4-alpine\nRUN printf 'target-test\\n' > /fixture-revision\n"), 0600); err != nil {
		t.Fatal(err)
	}
	git("add", "Dockerfile.gin")
	git("commit", "-m", "target fixture "+prefix)
	target := git("rev-parse", "HEAD")
	targetImage, err := e.Build(ctx, s, target, "web", "Dockerfile.gin")
	if err != nil {
		t.Fatal(err)
	}
	imageRefs = append(imageRefs, targetImage)
	current, err := e.Replace(ctx, snapshot, targetImage, target)
	if err != nil {
		t.Fatal(err)
	}
	if err = e.Healthy(ctx, current.ID); err != nil {
		t.Fatal(err)
	}
	if current.ID == snapshot.ID || current.Config["User"] != "10001:10001" || current.HostConfig["ReadonlyRootfs"] != true || current.label("frontiercloud.release-operation") != target {
		t.Fatal("replacement lost snapshot semantics")
	}
	if err = e.Exec(ctx, current.ID, []string{"/bin/sh", "-c", "grep -q target-test /fixture-revision && test \"$EXACT_ENV\" = preserved"}); err != nil {
		t.Fatal(err)
	}
}
