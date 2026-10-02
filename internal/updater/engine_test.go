package updater

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func sampleContainer() Container {
	c := Container{ID: strings.Repeat("a", 64), Name: "/private-web", Image: "sha256:" + strings.Repeat("b", 64), Config: map[string]any{"Cmd": []any{"serve"}, "Entrypoint": []any{"/app/frontiercloud"}, "Env": []any{"PASSWORD_FILE=/run/private/pass"}, "User": "10001:10001", "Labels": map[string]any{"com.docker.compose.project": "native-test", "com.docker.compose.service": "web"}, "Healthcheck": map[string]any{"Test": []any{"CMD", "/app/frontiercloud", "healthcheck"}}}, HostConfig: map[string]any{"Binds": []any{"private-data:/app/data:rw"}, "ReadonlyRootfs": true, "CapDrop": []any{"ALL"}, "Memory": json.Number("268435456"), "PortBindings": map[string]any{"8000/tcp": []any{map[string]any{"HostPort": "8000"}}}, "RestartPolicy": map[string]any{"Name": "always"}}}
	c.NetworkSettings.Networks = map[string]map[string]any{"private-net": {"Aliases": []any{c.ID, c.ID[:12], "web", "private-web"}, "IPAddress": "172.20.0.2", "IPAMConfig": map[string]any{"IPv4Address": "172.20.0.20"}}}
	return c
}

type redirectedTestTransport struct {
	base  *url.URL
	inner http.RoundTripper
}

func (t redirectedTestTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	q := r.Clone(r.Context())
	u := *r.URL
	u.Scheme, u.Host = t.base.Scheme, t.base.Host
	q.URL = &u
	return t.inner.RoundTrip(q)
}
func engineFixture(t *testing.T, handler http.HandlerFunc) *Engine {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	base, _ := url.Parse(server.URL)
	client := server.Client()
	client.Transport = redirectedTestTransport{base, client.Transport}
	return &Engine{client: client, version: "/v1.52"}
}

func TestSnapshotPreservesSecurityMountsResourcesAndConfiguredAliases(t *testing.T) {
	c := sampleContainer()
	body, err := c.createBody("frontiercloud-web:" + testTarget)
	if err != nil {
		t.Fatal(err)
	}
	if body["Image"] != "frontiercloud-web:"+testTarget || body["User"] != "10001:10001" {
		t.Fatal(body)
	}
	host := body["HostConfig"].(map[string]any)
	if host["ReadonlyRootfs"] != true || host["Memory"] != json.Number("268435456") || host["AutoRemove"] != false {
		t.Fatal(host)
	}
	network := body["NetworkingConfig"].(map[string]any)["EndpointsConfig"].(map[string]any)["private-net"].(map[string]any)
	aliases := network["Aliases"].([]string)
	if len(aliases) != 2 || aliases[0] != "web" || network["IPAddress"] != nil {
		t.Fatal(network)
	}
	host["ReadonlyRootfs"] = false
	if c.HostConfig["ReadonlyRootfs"] != true {
		t.Fatal("snapshot was mutated")
	}
}
func TestEngineUnixVersionNegotiationAndBoundedJSON(t *testing.T) {
	dir, err := os.MkdirTemp("", "fc-eng-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	socket := filepath.Join(dir, "docker.sock")
	l, err := net.Listen("unix", socket)
	if err != nil {
		t.Skip(err)
	}
	var mu sync.Mutex
	bad := false
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/version" {
			io.WriteString(w, `{"ApiVersion":"1.55","MinAPIVersion":"1.44"}`)
			return
		}
		mu.Lock()
		invalid := bad
		mu.Unlock()
		if invalid {
			io.WriteString(w, `{"Id":"a","Id":"b"}`)
			return
		}
		if r.URL.Path != "/v1.52/containers/private-web/json" {
			t.Error(r.URL.Path)
		}
		json.NewEncoder(w).Encode(sampleContainer())
	})}
	go server.Serve(l)
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	e, err := NewEngine(ctx, socket)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	if e.version != "/v1.52" {
		t.Fatal(e.version)
	}
	if _, err = e.Inspect(ctx, "private-web"); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	bad = true
	mu.Unlock()
	if _, err = e.Inspect(ctx, "private-web"); err == nil {
		t.Fatal("duplicate JSON admitted")
	}
}
func TestReplaceCannotDeleteUnrelatedSameNameContainer(t *testing.T) {
	c := sampleContainer()
	removed := false
	e := engineFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "DELETE" {
			removed = true
		}
		other := c
		other.ID = strings.Repeat("c", 64)
		json.NewEncoder(w).Encode(other)
	})
	if _, err := e.Replace(context.Background(), c, "frontiercloud-web:"+testTarget, testTarget); !errors.Is(err, ErrState) {
		t.Fatal(err)
	}
	if removed {
		t.Fatal("unrelated name removed")
	}
}
