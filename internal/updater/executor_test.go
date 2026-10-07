package updater

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// Stateful Engine contract fixture exercises the concrete executor's durable
// commit/recovery boundaries. Real Docker mechanics have a separate opt-in test.
type engineContract struct {
	mu                   sync.Mutex
	containers           map[string]Container
	images               map[string]Image
	execs                map[string]int
	commands             [][]string
	old, target, project string
	next                 int
	fault                string
	removed              []string
	cancelMutation       context.CancelFunc
}

func (f *engineContract) image(ref, revision, component string) Image {
	f.next++
	i := Image{ID: "sha256:" + fmt.Sprintf("%064x", f.next), RepoTags: []string{ref}}
	i.Config.Labels = map[string]string{"frontiercloud.revision": revision, "frontiercloud.component": component, "frontiercloud.runtime": "go", "frontiercloud.schema-generation": "2", "frontiercloud.project": f.project}
	if f.fault != "manifest-incompatible" || revision != f.target {
		i.Config.Labels["frontiercloud.release-manifest-version"] = "1"
	}
	f.images[ref], f.images[i.ID] = i, i
	return i
}
func (f *engineContract) container(name string) (Container, bool) {
	for _, c := range f.containers {
		if c.ID == name || strings.TrimPrefix(c.Name, "/") == name {
			return c, true
		}
	}
	return Container{}, false
}
func (f *engineContract) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	path := strings.TrimPrefix(r.URL.Path, "/v1.52")
	reply := func(v any) { json.NewEncoder(w).Encode(v) }
	if path == "/version" {
		reply(map[string]any{"ApiVersion": "1.52", "MinAPIVersion": "1.44"})
		return
	}
	if path == "/build" {
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			w.WriteHeader(500)
			return
		}
		var labels map[string]string
		json.Unmarshal([]byte(r.URL.Query().Get("labels")), &labels)
		ref := r.URL.Query().Get("t")
		i := f.image(ref, labels["frontiercloud.revision"], labels["frontiercloud.component"])
		reply(map[string]any{"aux": map[string]any{"ID": i.ID}})
		return
	}
	if strings.HasPrefix(path, "/images/") && strings.HasSuffix(path, "/json") {
		ref := strings.TrimSuffix(strings.TrimPrefix(path, "/images/"), "/json")
		i, ok := f.images[ref]
		if !ok {
			w.WriteHeader(404)
			return
		}
		reply(i)
		return
	}
	if path == "/images/json" {
		reply([]Image{})
		return
	}
	if path == "/containers/json" {
		var filters map[string][]string
		json.Unmarshal([]byte(r.URL.Query().Get("filters")), &filters)
		var rows []map[string]string
		for _, c := range f.containers {
			match := true
			for _, v := range filters["label"] {
				key, value, _ := strings.Cut(v, "=")
				if c.label(key) != value {
					match = false
				}
			}
			if match {
				rows = append(rows, map[string]string{"Id": c.ID})
			}
		}
		reply(rows)
		return
	}
	if path == "/containers/create" {
		var body map[string]any
		d := json.NewDecoder(r.Body)
		d.UseNumber()
		if d.Decode(&body) != nil {
			w.WriteHeader(400)
			return
		}
		name := r.URL.Query().Get("name")
		if _, ok := f.container(name); ok {
			w.WriteHeader(409)
			return
		}
		ref, _ := body["Image"].(string)
		image, ok := f.images[ref]
		if !ok {
			w.WriteHeader(404)
			return
		}
		if image.Config.Labels["frontiercloud.component"] == "updater" && image.Config.Labels["frontiercloud.revision"] == f.target && name == "private-updater" {
			if f.fault == "updater-create" || f.fault == "updater-create-cancel" {
				if f.cancelMutation != nil {
					f.cancelMutation()
				}
				w.WriteHeader(500)
				return
			}
		}
		f.next++
		c := Container{ID: fmt.Sprintf("%064x", f.next), Name: "/" + name, Image: image.ID, Config: cloneObject(body)}
		c.HostConfig, _ = c.Config["HostConfig"].(map[string]any)
		delete(c.Config, "HostConfig")
		delete(c.Config, "NetworkingConfig")
		c.State.Status = "created"
		c.NetworkSettings.Networks = sampleContainer().NetworkSettings.Networks
		f.containers[c.ID] = c
		if f.fault == "updater-create-lost" && image.Config.Labels["frontiercloud.component"] == "updater" && image.Config.Labels["frontiercloud.revision"] == f.target && name == "private-updater" {
			w.WriteHeader(500)
			return
		}
		reply(map[string]any{"Id": c.ID})
		return
	}
	if strings.HasPrefix(path, "/containers/") {
		tail := strings.TrimPrefix(path, "/containers/")
		name, action, _ := strings.Cut(tail, "/")
		c, ok := f.container(name)
		if !ok {
			w.WriteHeader(404)
			return
		}
		switch action {
		case "json":
			reply(c)
		case "start":
			if f.fault == "updater-start" && c.label("com.docker.compose.service") == "updater" && f.images[c.Image].Config.Labels["frontiercloud.revision"] == f.target {
				w.WriteHeader(500)
				return
			}
			c.State.Status = "running"
			c.State.Health.Status = "healthy"
			if c.label("frontiercloud.helper") != "" && strings.HasPrefix(c.label("frontiercloud.helper"), "fc-release-") {
				c.State.Status = "exited"
			}
			if f.fault == "health" && c.label("com.docker.compose.service") == "web" && f.images[c.Image].Config.Labels["frontiercloud.revision"] == f.target {
				c.State.Health.Status = "unhealthy"
			}
			f.containers[c.ID] = c
			if f.fault == "updater-start-lost" && c.label("com.docker.compose.service") == "updater" && f.images[c.Image].Config.Labels["frontiercloud.revision"] == f.target {
				w.WriteHeader(500)
				return
			}
			w.WriteHeader(204)
		case "stop":
			if c.State.Status == "exited" {
				w.WriteHeader(304)
				return
			}
			c.State.Status = "exited"
			f.containers[c.ID] = c
			w.WriteHeader(204)
		case "wait":
			reply(map[string]any{"StatusCode": 0})
		case "exec":
			var v struct{ Cmd []string }
			json.NewDecoder(r.Body).Decode(&v)
			f.commands = append(f.commands, append([]string(nil), v.Cmd...))
			f.next++
			id := fmt.Sprintf("%064x", f.next)
			code := 0
			if (f.fault == "nginx" && len(v.Cmd) > 2 && strings.Contains(v.Cmd[2], "nginx -t") && f.images[c.Image].Config.Labels["frontiercloud.revision"] == f.target) || (f.fault == "distribution" && len(v.Cmd) > 1 && (v.Cmd[1] == "cluster-release" || v.Cmd[1] == "cluster-release-manifest")) {
				code = 7
				if f.fault == "nginx" {
					c.State.Status = "exited"
					f.containers[c.ID] = c
				}
			}
			f.execs[id] = code
			reply(map[string]any{"Id": id})
		default:
			if r.Method == "DELETE" {
				if c.State.Status == "running" {
					w.WriteHeader(409)
					return
				}
				f.removed = append(f.removed, c.Name)
				delete(f.containers, c.ID)
				if f.fault == "updater-remove-lost" && c.label("com.docker.compose.service") == "updater" && f.images[c.Image].Config.Labels["frontiercloud.revision"] == f.old {
					f.fault = ""
					w.WriteHeader(500)
					return
				}
				w.WriteHeader(204)
			} else {
				w.WriteHeader(404)
			}
		}
		return
	}
	if strings.HasPrefix(path, "/exec/") {
		tail := strings.TrimPrefix(path, "/exec/")
		id, action, _ := strings.Cut(tail, "/")
		code, ok := f.execs[id]
		if !ok {
			w.WriteHeader(404)
			return
		}
		if action == "start" {
			w.WriteHeader(200)
			return
		}
		reply(map[string]any{"Running": false, "ExitCode": code})
		return
	}
	w.WriteHeader(404)
}
func executorFixture(t *testing.T, kind string) (*DockerExecutor, *engineContract, Status, string) {
	t.Helper()
	source, old, target := gitFixture(t)
	dir, err := os.MkdirTemp("", "fc-exec-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	socket := filepath.Join(dir, "d.sock")
	l, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	f := &engineContract{containers: map[string]Container{}, images: map[string]Image{}, execs: map[string]int{}, old: old, target: target, project: "native-test", fault: kind}
	for _, service := range []string{"web", "nginx", "updater", "secrets-init", "media-init"} {
		component := service
		if strings.HasSuffix(service, "-init") {
			component = "web"
		}
		ref := "initial-" + component
		i, ok := f.images[ref]
		if !ok {
			i = f.image(ref, old, component)
		}
		c := sampleContainer()
		f.next++
		c.ID = fmt.Sprintf("%064x", f.next)
		c.Name = "/private-" + service
		c.Image = i.ID
		c.Config["Labels"] = map[string]any{"com.docker.compose.project": f.project, "com.docker.compose.service": service}
		if service == "updater" {
			c.Config["Entrypoint"] = []any{"/app/frontiercloud-updater"}
		}
		c.State.Status = "running"
		c.State.Health.Status = "healthy"
		f.containers[c.ID] = c
	}
	server := &http.Server{Handler: f}
	go server.Serve(l)
	t.Cleanup(func() { server.Close() })
	control := filepath.Join(dir, "control")
	os.Mkdir(control, 0750)
	data := filepath.Join(dir, "data")
	os.Mkdir(data, 0755)
	os.WriteFile(filepath.Join(data, ".frontiercloud-force-open"), []byte("release-failure-override\n"), 0600)
	x := &DockerExecutor{Source: source, Socket: socket, Project: f.project, ControlDirectory: control, Runtime: old, DataDirectory: data}
	status := Status{State: "running", Phase: "validating", TargetSHA: target, CurrentSHA: old, RuntimeSHA: old, ReleaseBranch: "gin_main", Mode: "upgrade"}
	return x, f, status, target
}
func TestConcreteExecutorLocalFailureRestoresButDistributionRetainsGeneration(t *testing.T) {
	for _, kind := range []string{"health", "nginx", "distribution"} {
		t.Run(kind, func(t *testing.T) {
			x, f, status, target := executorFixture(t, kind)
			s, err := x.private()
			if err != nil {
				t.Fatal(err)
			}
			defer s.Close()
			progress := func(c Checkpoint) error {
				if c.State != "" {
					status.State = c.State
				}
				if c.Phase != "" {
					status.Phase = c.Phase
				}
				if c.Current != "" {
					status.CurrentSHA = c.Current
				}
				if c.Previous != "" || c.SetPrevious {
					status.PreviousSHA = c.Previous
				}
				return s.write("status.json", status, 8192)
			}
			if _, err = x.Execute(context.Background(), Request{Target: target, Mode: "upgrade", Hold: true}, status, progress); err == nil {
				t.Fatal("failure reported success")
			}
			f.mu.Lock()
			web, _ := f.container("private-web")
			revision := f.images[web.Image].Config.Labels["frontiercloud.revision"]
			f.mu.Unlock()
			want := f.old
			if kind == "distribution" {
				want = target
			}
			if revision != want || status.CurrentSHA != want {
				t.Fatalf("wrong commit boundary: image=%s status=%s want=%s", revision, status.CurrentSHA, want)
			}
			if _, err = os.Stat(filepath.Join(x.DataDirectory, ".frontiercloud-force-open")); !os.IsNotExist(err) {
				t.Fatal("force-open survived release")
			}
			var journal replacement
			if err = s.read("replacement.json", 16<<20, &journal); !os.IsNotExist(err) {
				t.Fatal("completed restore journal not drained", err)
			}
		})
	}
}
func TestConcreteUpdaterHandoffVerifiesImmutableRuntimeAndServices(t *testing.T) {
	x, f, status, target := executorFixture(t, "")
	s, err := x.private()
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	progress := func(c Checkpoint) error {
		if c.State != "" {
			status.State = c.State
		}
		if c.Phase != "" {
			status.Phase = c.Phase
		}
		if c.Current != "" {
			status.CurrentSHA = c.Current
		}
		if c.Previous != "" || c.SetPrevious {
			status.PreviousSHA = c.Previous
		}
		return s.write("status.json", status, 8192)
	}
	out, err := x.Execute(context.Background(), Request{Target: target, Mode: "upgrade"}, status, progress)
	if err != nil || !out.Handoff || status.State != "restarting" {
		t.Fatal(out, status, err)
	}
	if err = x.Handoff(context.Background()); err == nil {
		t.Fatal("old compiled runtime performed target handoff")
	}
	x.Runtime = target
	if err = x.Handoff(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err = x.Recover(context.Background(), status); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	current, _ := f.container("private-updater")
	revision := f.images[current.Image].Config.Labels["frontiercloud.revision"]
	f.mu.Unlock()
	if revision != target {
		t.Fatal("updater still old runtime")
	}
	var h handoff
	if err = s.read("handoff.json", 16<<20, &h); !os.IsNotExist(err) {
		t.Fatal("handoff not acknowledged", err)
	}
}
