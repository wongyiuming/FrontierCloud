package updater

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/release"
)

// Opt-in, full native stack. The caller supplies a newly created host directory
// mounted at the SAME absolute path into the test runner. Docker bind sources
// otherwise resolve on the host, not in the runner's private filesystem.
func TestRealNativeUpdaterUpgradeHandoffRollback(t *testing.T) {
	base := os.Getenv("FRONTIERCLOUD_TEST_UPDATER_WORKSPACE")
	socket := os.Getenv("FRONTIERCLOUD_TEST_DOCKER_SOCKET")
	if base == "" || socket == "" {
		t.Skip("isolated native updater stack not selected")
	}
	database := os.Getenv("FRONTIERCLOUD_TEST_UPDATER_DATABASE")
	if database == "" {
		database = "sqlite"
	}
	if database != "sqlite" && database != "mysql" {
		t.Fatal("unsupported isolated native store selection")
	}
	info, err := os.Lstat(base)
	if err != nil || !filepath.IsAbs(base) || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || !strings.HasPrefix(filepath.Base(base), "fc-native-updater-") {
		t.Fatal("explicit private host workspace required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	e, err := NewEngine(ctx, socket)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	var nonce [12]byte
	rand.Read(nonce[:])
	prefix := "fc-native-stack-" + hex.EncodeToString(nonce[:])
	e.Project = prefix
	diagnose := func() {
		probe, stop := context.WithTimeout(context.Background(), 10*time.Second)
		defer stop()
		var rows []struct {
			ID string `json:"Id"`
		}
		if e.call(probe, "GET", "/containers/json?all=true", nil, &rows) != nil {
			return
		}
		for _, row := range rows {
			c, err := e.Inspect(probe, row.ID)
			if err != nil || (c.label("com.docker.compose.project") != prefix && c.label("frontiercloud.project") != prefix) {
				continue
			}
			t.Log("private fixture state", c.Name, c.State.Status, c.State.ExitCode, c.State.Health.Status)
			if c.State.Status == "exited" || c.State.Health.Status == "unhealthy" {
				r, err := e.request(probe, "GET", "/containers/"+c.ID+"/logs?stdout=true&stderr=true&tail=20", nil, "")
				if err == nil {
					raw, _ := io.ReadAll(io.LimitReader(r.Body, 4096))
					r.Body.Close()
					t.Logf("private fixture diagnostic %q", raw)
				}
			}
		}
	}
	root := filepath.Join(base, prefix)
	if err = os.Mkdir(root, 0755); err != nil {
		t.Fatal(err)
	}
	sourceDir := filepath.Join(root, "source")
	if err = os.Mkdir(sourceDir, 0755); err != nil {
		t.Fatal(err)
	}
	nativeSource, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range archivePaths {
		from := filepath.Join(nativeSource, filepath.FromSlash(name))
		to := filepath.Join(sourceDir, filepath.FromSlash(name))
		info, err := os.Lstat(from)
		if err != nil {
			t.Fatal(err)
		}
		if info.IsDir() {
			source, err := os.OpenRoot(from)
			if err != nil {
				t.Fatal(err)
			}
			err = os.CopyFS(to, source.FS())
			source.Close()
			if err != nil {
				t.Fatal(err)
			}
		} else {
			os.MkdirAll(filepath.Dir(to), 0755)
			in, err := os.Open(from)
			if err != nil {
				t.Fatal(err)
			}
			out, err := os.OpenFile(to, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
			if err != nil {
				in.Close()
				t.Fatal(err)
			}
			_, err = io.Copy(out, in)
			in.Close()
			closeErr := out.Close()
			if err != nil || closeErr != nil {
				t.Fatal(err, closeErr)
			}
		}
	}
	git := func(args ...string) string {
		t.Helper()
		command := exec.CommandContext(ctx, "git", args...)
		command.Dir = sourceDir
		raw, err := command.CombinedOutput()
		if err != nil {
			t.Fatal(err, string(raw))
		}
		return strings.TrimSpace(string(raw))
	}
	git("init", "-b", "gin_main")
	git("config", "user.name", "Native Stack Test")
	git("config", "user.email", "native-stack@example.invalid")
	git("add", ".")
	git("commit", "-m", "isolated native base "+prefix)
	old := git("rev-parse", "HEAD")
	git("init", "--bare", ".fixture-origin")
	git("remote", "add", "origin", "./.fixture-origin")
	git("push", "origin", "gin_main")
	source := Source{Directory: sourceDir, Branch: "gin_main"}
	var images []string
	var names []string
	var networkID string
	defer func() {
		cleanup, stop := context.WithTimeout(context.Background(), 4*time.Minute)
		defer stop()
		// Stop the updater first, joining its worker before retiring its helpers.
		for _, service := range []string{"updater", "nginx", "web", "secrets-init", "media-init", "redis", "mysql"} {
			if c, err := e.Service(cleanup, prefix, service); err == nil {
				e.Stop(cleanup, c.ID)
				e.Remove(cleanup, c.ID)
			}
		}
		var rows []struct {
			ID string `json:"Id"`
		}
		e.call(cleanup, "GET", "/containers/json?all=true", nil, &rows)
		for _, row := range rows {
			if c, err := e.Inspect(cleanup, row.ID); err == nil && c.label("frontiercloud.project") == prefix && c.label("frontiercloud.helper") != "" {
				e.Stop(cleanup, c.ID)
				e.Remove(cleanup, c.ID)
			}
		}
		for _, name := range names {
			if c, err := e.Inspect(cleanup, name); err == nil && c.label("com.docker.compose.project") == prefix {
				e.Stop(cleanup, c.ID)
				e.Remove(cleanup, c.ID)
			}
		}
		for _, ref := range images {
			e.call(cleanup, "DELETE", "/images/"+ref+"?force=false&noprune=true", nil, nil)
		}
		if networkID != "" {
			e.call(cleanup, "DELETE", "/networks/"+networkID, nil, nil)
		}
		// The host owns this exact disposable child; leave it for caller cleanup
		// if container interruption kept a writer alive. Never recursively erase
		// a live bind or any repository/deployed data directory here.
	}()
	build := func(revision, component, file string) string {
		t.Helper()
		t.Log("build", component, revision)
		image, err := e.Build(ctx, source, revision, component, file)
		if err != nil {
			t.Fatal(err)
		}
		images = append(images, image)
		return image
	}
	webImage := build(old, "web", "Dockerfile.gin")
	nginxImage := build(old, "nginx", "nginx/Dockerfile")
	updaterImage := build(old, "updater", "updater/Dockerfile.gin")
	var network struct {
		ID string `json:"Id"`
	}
	if err = e.call(ctx, "POST", "/networks/create", map[string]any{"Name": prefix + "-net", "Internal": true}, &network); err != nil {
		t.Fatal(err)
	}
	networkID = network.ID
	for _, dir := range []string{"data", "secrets", "control", "maintenance", "redis", "mysql"} {
		if err = os.Mkdir(filepath.Join(root, dir), 0755); err != nil {
			t.Fatal(err)
		}
	}
	bind := func(dir, mount string, readOnly bool) string {
		mode := ":rw"
		if readOnly {
			mode = ":ro"
		}
		return filepath.Join(root, dir) + ":" + mount + mode
	}
	appBinds := []string{bind("data", "/app/data", false), bind("secrets", "/run/frontiercloud-secrets", false), bind("control", "/run/frontiercloud-updater", true)}
	appEnv := []string{"DB_TYPE=" + database, "SQLITE_PATH=/app/data/frontiercloud.db", "DATA_ROOT=/app/data", "REDIS_URL=redis://redis:6379/0", "RELEASE_BRANCH=gin_main", "RELEASE_SOURCE_BRANCH=gin_dev", "TLS_ENABLED=false", "MYSQL_HOST=mysql", "MYSQL_DATABASE=fc_native", "MYSQL_USER=media_admin"}
	create := func(service, image string, cmd []string, env, binds []string, user string, health bool, readonly bool, rootCaps bool) Container {
		t.Helper()
		name := prefix + "-" + service
		names = append(names, name)
		host := map[string]any{"Binds": binds, "NetworkMode": prefix + "-net", "ReadonlyRootfs": readonly, "CapDrop": []string{"ALL"}, "RestartPolicy": map[string]any{"Name": "no"}, "Tmpfs": map[string]string{"/tmp": "size=64m,mode=1777"}}
		// Retain the existing official Redis entrypoint's UID/GID setup; it is
		// an external service, not a Go app/helper inheriting CapDrop=ALL.
		if service == "redis" || service == "nginx" || service == "mysql" {
			host["CapDrop"] = []string{}
		}
		if rootCaps {
			host["CapAdd"] = []string{"CHOWN", "DAC_OVERRIDE", "FOWNER"}
		}
		body := map[string]any{"Image": image, "Cmd": cmd, "User": user, "Env": env, "HostConfig": host, "Labels": map[string]string{"com.docker.compose.project": prefix, "com.docker.compose.service": service}, "NetworkingConfig": map[string]any{"EndpointsConfig": map[string]any{prefix + "-net": map[string]any{"Aliases": []string{service}}}}}
		if health {
			body["Healthcheck"] = map[string]any{"Test": []string{"CMD", "/app/frontiercloud", "healthcheck"}, "Interval": int64(time.Second), "Timeout": int64(5 * time.Second), "Retries": 30, "StartPeriod": int64(2 * time.Second)}
			if service == "mysql" {
				body["Healthcheck"] = map[string]any{"Test": []string{"CMD-SHELL", "MYSQL_PWD=\"$(cat /run/frontiercloud-secrets/mysql_password)\" mysql -h 127.0.0.1 -u media_admin -e 'SELECT 1' fc_native"}, "Interval": int64(time.Second), "Timeout": int64(5 * time.Second), "Retries": 90, "StartPeriod": int64(60 * time.Second)}
			}
		}
		id, err := e.Create(ctx, name, body)
		if err != nil {
			t.Fatal(err)
		}
		if err = e.Start(ctx, id); err != nil {
			t.Fatal(err)
		}
		c, err := e.Inspect(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		return c
	}
	create("redis", "redis:7.4.11-alpine", []string{"redis-server", "--save", "", "--appendonly", "no"}, nil, []string{bind("redis", "/data", false)}, "", false, false, false)
	for _, service := range []string{"secrets-init", "media-init"} {
		command := "init-secrets"
		if service == "media-init" {
			command = "init-media"
		}
		c := create(service, webImage, []string{command}, appEnv, appBinds, "0:0", false, true, true)
		if err = e.Wait(ctx, c.ID); err != nil {
			t.Fatal(service, err)
		}
	}
	if database == "mysql" {
		mysql := create("mysql", "mysql:8.4.11", []string{"--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci"}, []string{"MYSQL_DATABASE=fc_native", "MYSQL_USER=media_admin", "MYSQL_PASSWORD_FILE=/run/frontiercloud-secrets/mysql_password", "MYSQL_ROOT_PASSWORD_FILE=/run/frontiercloud-secrets/mysql_root_password"}, []string{bind("mysql", "/var/lib/mysql", false), bind("secrets", "/run/frontiercloud-secrets", true)}, "", true, false, false)
		if err = e.Healthy(ctx, mysql.ID); err != nil {
			diagnose()
			t.Fatal("isolated MySQL initialization failed", err)
		}
	}
	web := create("web", webImage, []string{"serve"}, appEnv, appBinds, "10001:10001", true, true, false)
	if err = e.Healthy(ctx, web.ID); err != nil {
		diagnose()
		t.Fatal(err)
	}
	nginx := create("nginx", nginxImage, nil, []string{"TLS_ENABLED=false", "SERVER_NAME=localhost", "INSTANCE_NAME=native-test", "UPLOAD_INACTIVITY_TIMEOUT=300", "NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx"}, []string{bind("data", "/app/data", true), bind("maintenance", "/run/frontiercloud-maintenance", true)}, "", false, false, false)
	if err = e.NginxReady(ctx, nginx.ID); err != nil {
		diagnose()
		t.Fatal(err)
	}
	updaterEnv := []string{"UPDATER_PROJECT=" + prefix, "UPDATER_DATA_DIRECTORY=/data", "RELEASE_BRANCH=gin_main"}
	updaterBinds := []string{sourceDir + ":/workspace:rw", socket + ":/var/run/docker.sock:rw", bind("data", "/data", false), bind("control", "/run/frontiercloud-updater", false), bind("maintenance", "/run/frontiercloud-maintenance", false)}
	create("updater", updaterImage, []string{"serve"}, updaterEnv, updaterBinds, "0:0", false, true, true)
	agent := release.SocketAgent{Path: filepath.Join(root, "control", "control.sock")}
	wait := func(target, state string) map[string]any {
		t.Helper()
		for {
			if ctx.Err() != nil {
				t.Fatal("native updater deadline", ctx.Err())
			}
			out, err := agent.Request(ctx, map[string]any{"action": "status"})
			if err == nil {
				status, _ := out["status"].(map[string]any)
				if status["state"] == "failed" {
					diagnose()
					t.Fatal("native release failed", status)
				}
				if status["current_sha"] == target && status["state"] == state {
					return status
				}
			}
			time.Sleep(time.Second)
		}
	}
	wait(old, "idle")
	if err = e.Exec(ctx, web.ID, []string{"/app/frontiercloud", "updater-status"}); err != nil {
		diagnose()
		t.Fatal("unprivileged Web cannot reach control socket", err)
	}
	if err = os.WriteFile(filepath.Join(sourceDir, "static", "native-updater-fixture.txt"), []byte(prefix+"\n"), 0644); err != nil {
		t.Fatal(err)
	}
	git("add", "static/native-updater-fixture.txt")
	git("commit", "-m", "native target "+prefix)
	target := git("rev-parse", "HEAD")
	git("push", "origin", "gin_main")
	for _, component := range []string{"web", "nginx", "updater"} {
		images = append(images, "frontiercloud-"+component+":"+target)
	}
	out, err := agent.Request(ctx, map[string]any{"action": "start", "target_sha": target, "mode": "upgrade", "hold_maintenance": false})
	if err != nil || out["ok"] != true {
		t.Fatal(out, err)
	}
	status := wait(target, "success")
	if status["previous_sha"] != old || status["updater_runtime_sha"] != target {
		t.Fatal("handoff runtime proof missing", status)
	}
	if _, err = os.Stat(filepath.Join(root, "maintenance", "enabled")); !os.IsNotExist(err) {
		t.Fatal("successful release left public fence", err)
	}
	for _, service := range []string{"web", "nginx", "updater"} {
		c, err := e.Service(ctx, prefix, service)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = e.Image(ctx, c.Image, target, service); err != nil {
			t.Fatal(err)
		}
		if service == "web" || service == "updater" {
			if err = e.Exec(ctx, c.ID, []string{"/bin/sh", "-c", "! command -v python && ! command -v python3"}); err != nil {
				t.Fatal("native image contains Python interpreter", service, err)
			}
		}
	}
	out, err = agent.Request(ctx, map[string]any{"action": "start", "target_sha": old, "mode": "rollback", "hold_maintenance": false})
	if err != nil || out["ok"] != true {
		t.Fatal(out, err)
	}
	status = wait(old, "success")
	if status["previous_sha"] != "" || status["updater_runtime_sha"] != old {
		t.Fatal("rollback runtime proof missing", status)
	}
	if _, err = os.Stat(filepath.Join(root, "data", ".frontiercloud-native-maintenance")); !os.IsNotExist(err) {
		t.Fatal("native fence not resumed", err)
	}
	for _, service := range []string{"web", "nginx", "updater"} {
		c, err := e.Service(ctx, prefix, service)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = e.Image(ctx, c.Image, old, service); err != nil {
			t.Fatal(err)
		}
	}
	if database == "mysql" {
		if _, err = os.Stat(filepath.Join(root, "data", "frontiercloud.db")); !os.IsNotExist(err) {
			t.Fatal("MySQL runtime created an authoritative SQLite business database", err)
		}
	} else if _, err = e.Service(ctx, prefix, "mysql"); err == nil {
		t.Fatal("SQLite fixture launched a MySQL service")
	}
	t.Log("native stack upgrade, immutable updater handoff and rollback verified", prefix, database)
}
