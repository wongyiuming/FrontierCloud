package updater

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/release"
)

// Only the exact opt-in acceptance driver receives this private DNS alias. No
// host resolver, production verifier setting, repository, or public CA changes.
// The fixed authority and verified TLS remain real; reviewed CI replies below
// are explicitly synthetic publication fixtures, never production CI evidence.
type nativePublicationFixture struct {
	server          *http.Server
	done            chan error
	engine          *Engine
	network, driver string
	mu              sync.Mutex
	proofs          map[string]release.Artifact
	policies        map[string]release.Policy
	heads           map[string]string
	calls           map[string]int
}

func startNativePublicationFixture(t *testing.T, ctx context.Context, e *Engine, workspace, root, network string) *nativePublicationFixture {
	t.Helper()
	driver, err := e.Inspect(ctx, os.Getenv("HOSTNAME"))
	if err != nil || driver.label("frontiercloud.updater-acceptance") != workspace {
		t.Fatal("exact private publication driver ownership required", err)
	}
	folder := filepath.Join(root, "publication")
	if err := os.MkdirAll(filepath.Join(folder, "empty"), 0755); err != nil {
		t.Fatal(err)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	certificate := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "Private native publication fixture"}, DNSNames: []string{"api.github.com"}, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, certificate, certificate, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	private, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	ca := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	if err = os.WriteFile(filepath.Join(folder, "ca.pem"), ca, 0644); err != nil {
		t.Fatal(err)
	}
	pair, err := tls.X509KeyPair(ca, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: private}))
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", ":443")
	if err != nil {
		t.Fatal("private driver publication listener", err)
	}
	fixture := &nativePublicationFixture{proofs: map[string]release.Artifact{}, policies: map[string]release.Policy{}, heads: map[string]string{}, calls: map[string]int{}, done: make(chan error, 1)}
	fixture.server = &http.Server{Handler: http.HandlerFunc(fixture.reply), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 15 * time.Second, TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12, Certificates: []tls.Certificate{pair}}}
	go func() { fixture.done <- fixture.server.ServeTLS(listener, "", "") }()
	if err = e.call(ctx, "POST", "/networks/"+network+"/connect", map[string]any{"Container": driver.ID, "EndpointConfig": map[string]any{"Aliases": []string{"api.github.com"}}}, nil); err != nil {
		fixture.close()
		t.Fatal("private publication DNS attachment", err)
	}
	fixture.engine, fixture.network, fixture.driver = e, network, driver.ID
	return fixture
}

func (f *nativePublicationFixture) close() {
	_ = f.server.Close()
	<-f.done
	if f.engine != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		_ = f.engine.call(ctx, "POST", "/networks/"+f.network+"/disconnect", map[string]any{"Container": f.driver, "Force": false}, nil)
	}
}

func (f *nativePublicationFixture) manifest(t *testing.T, version, target string, git func(...string) string) *release.Manifest {
	t.Helper()
	tree := git("rev-parse", target+"^{tree}")
	// Actual, distinct private Git objects with the exact same tree. The fixture
	// simulates reviewed source/publication metadata, not an actual GitHub PR.
	source := git("commit-tree", tree, "-m", "private reviewed fixture "+version)
	reference := git("commit-tree", tree, "-m", "private reference fixture "+version)
	artifact := release.Artifact{Kind: "git-archive", CommitSHA: target, SourceSHA: source, TreeSHA: tree}
	f.mu.Lock()
	f.proofs[target] = artifact
	f.policies[target] = release.Policy{Branch: "gin_main", Source: "gin_dev"}
	f.heads["gin_main"] = target
	f.mu.Unlock()
	return &release.Manifest{Format: "frontiercloud-release-manifest", Version: 1, ReleaseVersion: version, Protocol: 2, SchemaGeneration: 2, Artifacts: map[string]release.Artifact{"gin_main": artifact, "main": {Kind: "git-archive", CommitSHA: reference, SourceSHA: source, TreeSHA: tree}}}
}

func (f *nativePublicationFixture) reply(w http.ResponseWriter, r *http.Request) {
	if r.Method != "GET" || r.Host != "api.github.com" || !strings.HasPrefix(r.URL.Path, "/repos/wongyiuming/FrontierCloud/") {
		http.Error(w, "private fixture scope", 404)
		return
	}
	path := strings.TrimPrefix(r.URL.Path, "/repos/wongyiuming/FrontierCloud")
	f.mu.Lock()
	defer f.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	for target, artifact := range f.proofs {
		policy := f.policies[target]
		if !policy.Valid() {
			policy = release.Policy{Branch: "gin_main", Source: "gin_dev"}
		}
		var reply any
		switch {
		case path == "/branches/"+policy.Branch && f.heads[policy.Branch] == target:
			reply = map[string]any{"commit": map[string]any{"sha": target, "commit": map[string]any{"tree": map[string]any{"sha": artifact.TreeSHA}}}}
		case path == "/commits/"+target || path == "/commits/"+artifact.SourceSHA:
			sha := strings.TrimPrefix(path, "/commits/")
			reply = map[string]any{"sha": sha, "commit": map[string]any{"tree": map[string]any{"sha": artifact.TreeSHA}}}
		case path == "/commits/"+target+"/pulls" && r.URL.Query().Get("per_page") == "100":
			reply = []any{map[string]any{"merged_at": "2026-10-04T00:00:00Z", "merge_commit_sha": target, "base": map[string]any{"ref": policy.Branch}, "head": map[string]any{"ref": policy.Source, "sha": artifact.SourceSHA, "repo": map[string]any{"full_name": "wongyiuming/FrontierCloud"}}}}
		case path == "/actions/workflows/docker.yml/runs" && r.URL.Query().Get("event") == "push" && r.URL.Query().Get("head_sha") == artifact.SourceSHA && r.URL.Query().Get("per_page") == "20":
			reply = map[string]any{"workflow_runs": []any{map[string]any{"head_branch": policy.Source, "head_sha": artifact.SourceSHA, "event": "push", "run_number": 1, "status": "completed", "conclusion": "success"}}}
		}
		if reply != nil {
			f.calls[target+path]++
			_ = json.NewEncoder(w).Encode(reply)
			return
		}
	}
	http.Error(w, "unknown private fixture proof", 404)
}

func (f *nativePublicationFixture) assertProofs(t *testing.T, targets ...string) {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, target := range targets {
		artifact := f.proofs[target]
		for _, path := range []string{"/commits/" + target, "/commits/" + target + "/pulls", "/commits/" + artifact.SourceSHA, "/actions/workflows/docker.yml/runs"} {
			if f.calls[target+path] == 0 {
				t.Fatal("compiled agent skipped independent private publication proof", path)
			}
		}
	}
}

func nativeManifestStart(manifest *release.Manifest, mode string) map[string]any {
	return map[string]any{"action": "start", "release_manifest": manifest, "mode": mode, "hold_maintenance": false}
}

func nativeManifestStatus(t *testing.T, status map[string]any, current, previous *release.Manifest) {
	t.Helper()
	for field, expected := range map[string]*release.Manifest{"current_manifest": current, "previous_manifest": previous} {
		if expected == nil {
			if status[field] != nil {
				t.Fatal("unexpected whole-release history", field)
			}
			continue
		}
		actual, err := release.ManifestFromValue(status[field])
		got, _ := actual.ID()
		want, _ := expected.ID()
		if err != nil || got != want {
			t.Fatal("whole-release history did not survive real execution/handoff", field, err)
		}
	}
}

func TestPrivatePublicationFixtureScopesExactArtifactRequests(t *testing.T) {
	target, source, tree := strings.Repeat("a", 40), strings.Repeat("b", 40), strings.Repeat("c", 40)
	f := &nativePublicationFixture{proofs: map[string]release.Artifact{target: {Kind: "git-archive", CommitSHA: target, SourceSHA: source, TreeSHA: tree}}, calls: map[string]int{}}
	for _, check := range []struct {
		method, host, path string
		want               int
	}{
		{"GET", "api.github.com", "/repos/wongyiuming/FrontierCloud/commits/" + target, 200},
		{"GET", "api.github.com", "/repos/wongyiuming/FrontierCloud/commits/" + source, 200},
		{"GET", "api.github.com", "/repos/wongyiuming/FrontierCloud/commits/" + target + "/pulls?per_page=100", 200},
		{"GET", "api.github.com", "/repos/wongyiuming/FrontierCloud/actions/workflows/docker.yml/runs?event=push&head_sha=" + source + "&per_page=20", 200},
		{"POST", "api.github.com", "/repos/wongyiuming/FrontierCloud/commits/" + target, 404},
		{"GET", "foreign.invalid", "/repos/wongyiuming/FrontierCloud/commits/" + target, 404},
		{"GET", "api.github.com", "/repos/foreign/FrontierCloud/commits/" + target, 404},
		{"GET", "api.github.com", "/repos/wongyiuming/FrontierCloud/commits/" + tree, 404},
		{"GET", "api.github.com", "/repos/wongyiuming/FrontierCloud/commits/" + target + "/pulls?per_page=1", 404},
		{"GET", "api.github.com", "/repos/wongyiuming/FrontierCloud/actions/workflows/docker.yml/runs?event=push&head_sha=" + target + "&per_page=20", 404},
		{"GET", "api.github.com", "/repos/wongyiuming/FrontierCloud/actions/workflows/docker.yml/runs?event=push&head_sha=" + source + "&per_page=100", 404},
	} {
		r := httptest.NewRequest(check.method, "https://"+check.host+check.path, nil)
		w := httptest.NewRecorder()
		f.reply(w, r)
		if w.Code != check.want {
			t.Fatal("private publication scope mismatch", check.method, check.host, check.path, w.Code)
		}
	}
	f.assertProofs(t, target)
}

func TestPrivatePublicationFixtureSeparatesJointProfilesAndCurrentHeads(t *testing.T) {
	f := &nativePublicationFixture{proofs: map[string]release.Artifact{}, policies: map[string]release.Policy{}, heads: map[string]string{}, calls: map[string]int{}}
	for i, policy := range []release.Policy{release.DefaultPolicy(), {Branch: "gin_main", Source: "gin_dev"}} {
		artifact := release.Artifact{Kind: "git-archive", CommitSHA: strings.Repeat(string(rune('a'+i)), 40), SourceSHA: strings.Repeat(string(rune('c'+i)), 40), TreeSHA: strings.Repeat(string(rune('e'+i)), 40)}
		f.proofs[artifact.CommitSHA], f.policies[artifact.CommitSHA], f.heads[policy.Branch] = artifact, policy, artifact.CommitSHA
	}
	for target, artifact := range f.proofs {
		policy := f.policies[target]
		for _, path := range []string{"/branches/" + policy.Branch, "/commits/" + target + "/pulls?per_page=100", "/actions/workflows/docker.yml/runs?event=push&head_sha=" + artifact.SourceSHA + "&per_page=20"} {
			r := httptest.NewRequest("GET", "https://api.github.com/repos/wongyiuming/FrontierCloud"+path, nil)
			w := httptest.NewRecorder()
			f.reply(w, r)
			if w.Code != 200 {
				t.Fatal("missing joint private publication proof", path)
			}
			var value any
			if err := json.Unmarshal(w.Body.Bytes(), &value); err != nil {
				t.Fatal(err)
			}
			switch {
			case strings.HasPrefix(path, "/branches/"):
				commit := value.(map[string]any)["commit"].(map[string]any)
				if commit["sha"] != target {
					t.Fatal("private current HEAD crossed profiles")
				}
			case strings.Contains(path, "/pulls"):
				pull := value.([]any)[0].(map[string]any)
				if pull["base"].(map[string]any)["ref"] != policy.Branch || pull["head"].(map[string]any)["ref"] != policy.Source {
					t.Fatal("private reviewed source crossed profiles")
				}
			default:
				run := value.(map[string]any)["workflow_runs"].([]any)[0].(map[string]any)
				if run["head_branch"] != policy.Source || run["head_sha"] != artifact.SourceSHA {
					t.Fatal("private CI selection crossed profiles")
				}
			}
		}
	}
}
