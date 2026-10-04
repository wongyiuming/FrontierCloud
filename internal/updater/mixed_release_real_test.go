package updater

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/release"
)

type fleetJointRelease struct {
	publication                                             *nativePublicationFixture
	source                                                  *jointSourceFixture
	initial                                                 *release.Manifest
	metadata, native, agent, reference, edge, referenceEdge string
}

// Only private synthetic fixture failures are inspected. Drop credential-bearing
// lines and opaque values before bounding output; never publish raw status/logs.
func fleetFailureDiagnostic(detail string) string {
	opaque := regexp.MustCompile(`[A-Za-z0-9_+/-]{32,}={0,2}`)
	lines := []string{}
	for _, line := range strings.Split(detail, "\n") {
		lower := strings.ToLower(line)
		private := false
		for _, field := range []string{"password", "secret", "credential", "authorization", "cookie", "token", "private_key", "pair_package"} {
			private = private || strings.Contains(lower, field)
		}
		if private {
			lines = append(lines, "[private diagnostic line omitted]")
		} else {
			lines = append(lines, opaque.ReplaceAllString(line, "[opaque value omitted]"))
		}
	}
	result := strings.Join(lines, "\n")
	if len(result) > 2048 {
		result = result[:2048] + " [bounded]"
	}
	return result
}

func retireFleetProjects(t *testing.T, ctx context.Context, e *Engine, owned map[string]bool) {
	t.Helper()
	// Python as PID 1 can consume Docker's full stop grace period. A barrier
	// stops all agents first, then a fresh inventory catches their last helpers.
	// Concurrency is bounded; no force removal, volumes or global prune.
	for _, agents := range []bool{true, false} {
		var rows []struct {
			ID string `json:"Id"`
		}
		if err := e.call(ctx, "GET", "/containers/json?all=true", nil, &rows); err != nil {
			t.Error("private fleet cleanup inventory", err)
			return
		}
		var wait sync.WaitGroup
		slots := make(chan struct{}, 8)
		for _, row := range rows {
			c, err := e.Inspect(ctx, row.ID)
			if err != nil || !(owned[c.label("com.docker.compose.project")] || owned[c.label("frontiercloud.project")]) || (c.label("com.docker.compose.service") == "updater") != agents {
				continue
			}
			slots <- struct{}{}
			wait.Go(func() {
				defer func() { <-slots }()
				if err := e.Stop(ctx, c.ID); err != nil {
					t.Error("private fleet stop", err)
					return
				}
				if err := e.Remove(ctx, c.ID); err != nil {
					t.Error("private fleet retirement", err)
				}
			})
		}
		wait.Wait()
	}
}

func fleetReleaseService(project, service string) (string, string) {
	label, suffix, ok := strings.Cut(service, "-")
	if !ok || len(label) != 5 || !strings.HasPrefix(label, "site") || label[4] < '0' || label[4] > '9' {
		return project, service
	}
	canonical := map[string]string{"web": "web", "edge": "nginx", "updater": "updater", "init-secrets": "secrets-init", "init-media": "media-init"}[suffix]
	if canonical == "" {
		return project, service
	}
	return project + "-" + label, canonical
}

func prepareFleetJointRelease(t *testing.T, ctx context.Context, e *Engine, workspace, root, project, network, certificates string) *fleetJointRelease {
	t.Helper()
	driver, err := e.Inspect(ctx, os.Getenv("HOSTNAME"))
	if err != nil || driver.label("frontiercloud.updater-acceptance") != workspace {
		t.Fatal("exact private mixed release driver required", err)
	}
	pair, err := tls.LoadX509KeyPair(filepath.Join(certificates, "fullchain.pem"), filepath.Join(certificates, "privkey.pem"))
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", ":443")
	if err != nil {
		t.Fatal(err)
	}
	publication := &nativePublicationFixture{proofs: map[string]release.Artifact{}, policies: map[string]release.Policy{}, heads: map[string]string{}, calls: map[string]int{}, done: make(chan error, 1)}
	publication.server = &http.Server{Handler: http.HandlerFunc(publication.reply), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 15 * time.Second, TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12, Certificates: []tls.Certificate{pair}}}
	go func() { publication.done <- publication.server.ServeTLS(listener, "", "") }()
	if err = e.call(ctx, "POST", "/networks/"+network+"/connect", map[string]any{"Container": driver.ID, "EndpointConfig": map[string]any{"Aliases": []string{"api.github.com"}}}, nil); err != nil {
		publication.close()
		t.Fatal(err)
	}
	publication.engine, publication.network, publication.driver = e, network, driver.ID
	// Close the isolated listener even if source/archive/image setup fails.
	t.Cleanup(func() { publication.closeOnce() })
	sourceRoot, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	source := newJointSourceFixture(t, ctx, sourceRoot, root, publication)
	f := &fleetJointRelease{source: source, publication: publication, initial: release.CloneManifest(source.current), metadata: filepath.Join(root, "publication")}
	if err = os.Mkdir(f.metadata, 0755); err != nil {
		t.Fatal(err)
	}
	f.writeManifest(t, f.initial)
	previousProject := e.Project
	e.Project = project
	defer func() { e.Project = previousProject }()
	for component, recipe := range map[string]string{"web": "Dockerfile.gin", "updater": "updater/Dockerfile.gin", "nginx": "nginx/Dockerfile"} {
		t.Log("private whole-release baseline build", component)
		image, err := e.Build(ctx, Source{Directory: source.directory, Branch: "gin_main"}, f.initial.Artifacts["gin_main"].CommitSHA, component, recipe)
		if err != nil {
			t.Fatal("private native release baseline", component, err)
		}
		switch component {
		case "web":
			f.native = image
		case "updater":
			f.agent = image
		case "nginx":
			f.edge = image
		}
	}
	t.Log("private whole-release baseline build reference Web")
	f.reference = f.buildReference(t, ctx, e)
	t.Log("private whole-release baseline build reference edge")
	f.referenceEdge, err = e.Build(ctx, Source{Directory: source.directory, Branch: "main"}, f.initial.Artifacts["main"].CommitSHA, "nginx", "nginx/Dockerfile")
	if err != nil {
		t.Fatal("private reference edge baseline", err)
	}
	return f
}

func (f *fleetJointRelease) writeManifest(t *testing.T, m *release.Manifest) {
	t.Helper()
	raw, err := m.Wire()
	if err != nil {
		t.Fatal(err)
	}
	temporary := filepath.Join(f.metadata, "next.json")
	if err = os.WriteFile(temporary, raw, 0644); err != nil {
		t.Fatal(err)
	}
	if err = os.Rename(temporary, filepath.Join(f.metadata, "current.json")); err != nil {
		t.Fatal(err)
	}
}

func (f *fleetJointRelease) buildReference(t *testing.T, ctx context.Context, e *Engine) string {
	t.Helper()
	target := f.initial.Artifacts["main"].CommitSHA
	arguments := append([]string{"archive", "--format=tar", target, "--"}, jointSourcePaths...)
	command := exec.CommandContext(ctx, "git", arguments...)
	command.Dir = f.source.directory
	command.Stderr = io.Discard
	stream, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err = command.Start(); err != nil {
		stream.Close()
		t.Fatal(err)
	}
	archive := &archiveStream{ReadCloser: stream, cmd: command}
	defer archive.Close()
	tag := releaseImageTag(e.Project, target, "web")
	labels, _ := json.Marshal(map[string]string{"frontiercloud.revision": target, "frontiercloud.component": "web", "frontiercloud.runtime": "python", "frontiercloud.project": e.Project, "frontiercloud.schema-generation": "2"})
	query := url.Values{"dockerfile": {"Dockerfile.python"}, "t": {tag}, "labels": {string(labels)}, "rm": {"true"}, "forcerm": {"true"}, "version": {"1"}}
	response, err := e.request(ctx, "POST", "/build?"+query.Encode(), archive, "application/x-tar")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	reader := bufio.NewReaderSize(response.Body, 1<<20)
	for {
		line, err := reader.ReadSlice('\n')
		if err == io.EOF && len(line) == 0 {
			break
		}
		if err != nil && err != io.EOF {
			t.Fatal("private reference build stream", err)
		}
		if len(bytes.TrimSpace(line)) != 0 {
			value, parseErr := protocol.ParseStrictJSON(line, 1<<20)
			row, ok := value.(map[string]any)
			if parseErr != nil || !ok || row["error"] != nil || row["errorDetail"] != nil {
				t.Fatal("private reference archive build failed")
			}
		}
		if err == io.EOF {
			break
		}
	}
	if err = archive.Close(); err != nil {
		t.Fatal(err)
	}
	var image Image
	if err = e.call(ctx, "GET", "/images/"+tag+"/json", nil, &image); err != nil || image.Config.Labels["frontiercloud.revision"] != target || image.Config.Labels["frontiercloud.release-manifest-version"] != "1" {
		t.Fatal("private reference image proof failed", err)
	}
	return tag
}

func (f *fleetJointRelease) execute(t *testing.T, ctx context.Context, e *Engine, sites []*fleetSite, perform func(*fleetSite, string, string, any) map[string]any) {
	t.Helper()
	identities := make([]string, len(sites))
	for i, site := range sites {
		identities[i], _ = perform(site, "GET", "/nodes", nil)["node_id"].(string)
	}
	// Begin through the real authenticated Master Admin endpoint. Every follower
	// is driven by the actual signed RPC and its own isolated Engine agent.
	for _, step := range []struct {
		manifest *release.Manifest
		mode     string
	}{{f.initial, "upgrade"}, {nil, "upgrade"}, {f.initial, "rollback"}} {
		m := step.manifest
		if m == nil {
			m = f.source.next(t)
			f.writeManifest(t, m)
		}
		releaseStarted := time.Now().Unix()
		if step.mode == "upgrade" {
			// Publication HEAD verification deliberately retains a one-minute
			// forced-refresh floor. Await admission; never bypass its cache or
			// turn a stale proof into permission to queue a new manifest.
			readyDeadline := time.Now().Add(2 * time.Minute)
			for {
				status := perform(sites[0], "GET", "/nodes/release?refresh_ci=true", nil)
				if status["can_upgrade"] == true && status["release_policy_ready"] == true {
					break
				}
				if ctx.Err() != nil || time.Now().After(readyDeadline) {
					ci, _ := status["ci"].(map[string]any)
					t.Fatal("real mixed published release not ready", "policy", status["release_policy_ready"], "publication", ci["publishable"], "convergence", status["cluster_convergence_needed"])
				}
				time.Sleep(5 * time.Second)
			}
		}
		result := perform(sites[0], "POST", "/nodes/release/"+step.mode, map[string]any{})
		id, _ := m.ID()
		if result["release_id"] != id {
			t.Fatal("Master did not acknowledge whole release")
		}
		deadline := time.Now().Add(20 * time.Minute)
		nextProgress := time.Now()
		for {
			complete := true
			failed := false
			phases := map[string]int{}
			for _, site := range sites {
				out, err := (release.SocketAgent{Path: filepath.Join(site.root, "updater-control", "control.sock")}).Request(ctx, map[string]any{"action": "status"})
				status, _ := out["status"].(map[string]any)
				phase, _ := status["phase"].(string)
				phases[site.runtime+"/"+phase]++
				if err == nil && status["state"] == "failed" {
					if site.runtime == "go" {
						t.Log("bounded native failure classification", status["detail"])
					} else if detail, ok := status["detail"].(string); ok {
						t.Log("bounded reference failure diagnostic", fleetFailureDiagnostic(detail))
					}
					t.Log("real mixed whole release failed", site.project, site.runtime, site.database, status["phase"])
					failed = true
				}
				actual, parseErr := release.ManifestFromValue(status["current_manifest"])
				got, _ := actual.ID()
				branch := "gin_main"
				if site.runtime == "python" {
					branch = "main"
				}
				complete = complete && err == nil && parseErr == nil && got == id && status["state"] == "success" && status["current_sha"] == m.Artifacts[branch].CommitSHA && status["updater_runtime_sha"] == m.Artifacts[branch].CommitSHA
			}
			if failed {
				// Preserve the primary cause before cleanup signals agents that
				// are still recovering. Socket absence is not a failed release.
				for _, site := range sites {
					if site.runtime != "go" {
						continue
					}
					var persisted Status
					s, openErr := openPrivate(filepath.Join(site.root, "updater-control"), false)
					if openErr == nil {
						readErr := s.read("status.json", 8192, &persisted)
						s.Close()
						if readErr == nil && persisted.valid() {
							t.Log("native pre-cleanup persisted state", site.project, persisted.State, persisted.Phase, persisted.Detail)
						}
					}
				}
				t.Fatal("real mixed whole release failed; fleet classification", phases)
			}
			if complete {
				break
			}
			if time.Now().After(nextProgress) {
				t.Log("private whole-release convergence", step.mode, m.ReleaseVersion, phases)
				nextProgress = time.Now().Add(30 * time.Second)
			}
			if ctx.Err() != nil || time.Now().After(deadline) {
				t.Fatal("real mixed whole release convergence deadline")
			}
			time.Sleep(4 * time.Second)
		}
		for i, site := range sites {
			web, err := e.Service(ctx, site.project, "web")
			if err != nil {
				t.Fatal(err)
			}
			site.web = web.ID
			if err = e.Healthy(ctx, web.ID); err != nil {
				t.Fatal("whole release Web not healthy", err)
			}
			if id, _ := perform(site, "GET", "/nodes", nil)["node_id"].(string); id != identities[i] {
				t.Fatal("whole release changed node identity")
			}
			branch := "gin_main"
			if site.runtime == "python" {
				branch = "main"
			}
			for _, service := range []string{"web", "nginx"} {
				container, err := e.Service(ctx, site.project, service)
				if err != nil {
					t.Fatal(err)
				}
				var image Image
				if err = e.call(ctx, "GET", "/images/"+container.Image+"/json", nil, &image); err != nil || image.Config.Labels["frontiercloud.revision"] != m.Artifacts[branch].CommitSHA {
					t.Fatal("mixed artifact/image divergence", service, err)
				}
			}
			if site.runtime == "go" {
				if err = e.Exec(ctx, web.ID, []string{"sh", "-c", "! command -v python && ! command -v python3"}); err != nil {
					t.Fatal("mixed release added Python to native Web")
				}
			}
		}
		// Agent/image convergence is not peer readiness. A simultaneous release
		// can finish between a failed outbound probe and the next 30s heartbeat.
		// Require actual fresh authenticated reachability and all original stores,
		// without retrying or weakening the subsequent business-byte assertions.
		readyDeadline := time.Now().Add(90 * time.Second)
		for {
			status := perform(sites[0], "GET", "/nodes", nil)
			pool := perform(sites[0], "GET", "/storage-pool", nil)
			if fleetBusinessReady(status, pool, identities, releaseStarted) {
				break
			}
			if ctx.Err() != nil || time.Now().After(readyDeadline) {
				t.Fatal("whole release did not recover fresh heartbeats and all original storage members", step.mode, m.ReleaseVersion)
			}
			time.Sleep(time.Second)
		}
		t.Log("real mixed whole manifest, privately selected artifacts, compiled/live agents and storage heartbeat convergence", step.mode, m.ReleaseVersion)
	}
	for _, m := range []*release.Manifest{f.initial, f.source.current} {
		for _, a := range m.Artifacts {
			f.publication.assertProofs(t, a.CommitSHA)
		}
	}
}

func fleetBusinessReady(status, pool map[string]any, identities []string, since int64) bool {
	rows, ok := status["relationships"].([]any)
	if !ok || len(rows) != len(identities)-1 {
		return false
	}
	peers := map[string]bool{}
	for _, raw := range rows {
		row, ok := raw.(map[string]any)
		if !ok {
			return false
		}
		peer, _ := row["peer_id"].(string)
		last, ok := row["last_heartbeat"].(json.Number)
		stamp, err := last.Int64()
		if !ok || err != nil || stamp < since || row["state"] != "active" || row["status"] != "online" || peer == "" || peers[peer] {
			return false
		}
		peers[peer] = true
	}
	members, ok := pool["members"].([]any)
	if !ok {
		return false
	}
	stores := map[string]bool{}
	for _, raw := range members {
		member, ok := raw.(map[string]any)
		if !ok {
			return false
		}
		id, _ := member["member_id"].(string)
		if id != "" && member["health"] == "online" {
			stores[id] = true
		}
	}
	for i, id := range identities {
		if id == "" || !stores[id] || i > 0 && !peers[id] {
			return false
		}
	}
	return true
}

func TestFleetBusinessReadyRequiresFreshReachabilityAndOriginalStores(t *testing.T) {
	identities := []string{"master", "follower"}
	relation := map[string]any{"peer_id": "follower", "state": "active", "status": "online", "last_heartbeat": json.Number("100")}
	member := map[string]any{"member_id": "follower", "health": "online"}
	status := map[string]any{"relationships": []any{relation}}
	pool := map[string]any{"members": []any{map[string]any{"member_id": "master", "health": "online"}, member}}
	if !fleetBusinessReady(status, pool, identities, 100) {
		t.Fatal("fresh original fleet rejected")
	}
	for _, field := range []string{"status", "state", "last_heartbeat", "peer_id"} {
		old := relation[field]
		relation[field] = map[string]any{"status": "offline", "state": "revoked", "last_heartbeat": json.Number("99"), "peer_id": "replacement"}[field]
		if fleetBusinessReady(status, pool, identities, 100) {
			t.Fatal("unready or replaced peer accepted", field)
		}
		relation[field] = old
	}
	member["health"] = "offline"
	if fleetBusinessReady(status, pool, identities, 100) {
		t.Fatal("offline recording store accepted")
	}
}

func TestFleetFailureDiagnosticDoesNotExposePrivateValues(t *testing.T) {
	key := strings.Repeat("k", 48)
	raw := "RuntimeError: nginx configuration check failed\npassword=" + key + "\nopaque=" + key
	got := fleetFailureDiagnostic(raw)
	if strings.Contains(got, key) || !strings.Contains(got, "nginx configuration check failed") || len(fleetFailureDiagnostic(strings.Repeat("!", 9000))) > 2100 {
		t.Fatal("private fleet diagnostics were not safely bounded")
	}
}

func TestFleetReleaseServiceOwnershipIsExact(t *testing.T) {
	for _, check := range []struct{ service, owner, canonical string }{
		{"site0-web", "private-site0", "web"}, {"site9-edge", "private-site9", "nginx"}, {"site2-init-media", "private-site2", "media-init"}, {"site1-init-secrets", "private-site1", "secrets-init"}, {"site2-updater", "private-site2", "updater"},
		{"site10-web", "private", "site10-web"}, {"backup-proof-site2", "private", "backup-proof-site2"}, {"mysql", "private", "mysql"}, {"site1-unknown", "private", "site1-unknown"},
	} {
		owner, canonical := fleetReleaseService("private", check.service)
		if owner != check.owner || canonical != check.canonical {
			t.Fatal("private release ownership selector mismatch", check.service)
		}
	}
}

func TestFleetRetirementHasAgentBarrierAndFreshOwnedHelperInventory(t *testing.T) {
	ids := map[string]string{"agent": strings.Repeat("a", 64), "web": strings.Repeat("b", 64), "foreign": strings.Repeat("c", 64), "helper": strings.Repeat("d", 64)}
	rows := map[string]Container{}
	for name, id := range ids {
		labels := map[string]any{"com.docker.compose.project": "private-site0", "com.docker.compose.service": name}
		if name == "agent" {
			labels["com.docker.compose.service"] = "updater"
		}
		if name == "foreign" {
			labels["com.docker.compose.project"] = "private-site00"
		}
		if name == "helper" {
			labels = map[string]any{"frontiercloud.project": "private-site0", "frontiercloud.helper": "late-helper"}
		}
		c := sampleContainer()
		c.ID = id
		c.Config["Labels"] = labels
		rows[id] = c
	}
	var mu sync.Mutex
	retired := map[string]bool{}
	late, agentRemoved, inventories := false, false, 0
	e := engineFixture(t, func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		path := strings.TrimPrefix(r.URL.Path, "/v1.52")
		if r.Method == "GET" && path == "/containers/json" {
			inventories++
			if inventories == 2 && !agentRemoved {
				t.Error("service inventory ran before agent retirement")
			}
			var out []map[string]string
			for id := range rows {
				if !retired[id] && (id != ids["helper"] || late) {
					out = append(out, map[string]string{"Id": id})
				}
			}
			_ = json.NewEncoder(w).Encode(out)
			return
		}
		parts := strings.Split(strings.Trim(path, "/"), "/")
		if len(parts) < 2 || parts[0] != "containers" {
			http.Error(w, "unexpected test route", 400)
			return
		}
		id := parts[1]
		if r.Method == "GET" {
			_ = json.NewEncoder(w).Encode(rows[id])
			return
		}
		if id == ids["foreign"] {
			t.Error("foreign lookalike container mutation")
		}
		if r.Method == "POST" && len(parts) == 3 && parts[2] == "stop" {
			if id == ids["agent"] {
				late = true
			} else if !agentRemoved {
				t.Error("service stopped before agent barrier")
			}
			w.WriteHeader(204)
			return
		}
		if r.Method == "DELETE" && len(parts) == 2 {
			if r.URL.Query().Get("force") != "false" || r.URL.Query().Get("v") != "false" {
				t.Error("forced or volume retirement")
			}
			retired[id] = true
			if id == ids["agent"] {
				agentRemoved = true
			}
			w.WriteHeader(204)
			return
		}
		http.Error(w, "unexpected test mutation", 400)
	})
	retireFleetProjects(t, context.Background(), e, map[string]bool{"private-site0": true})
	if inventories != 2 || !retired[ids["agent"]] || !retired[ids["web"]] || !retired[ids["helper"]] || retired[ids["foreign"]] {
		t.Fatal("private retirement lost barrier, helper or ownership proof")
	}
}
