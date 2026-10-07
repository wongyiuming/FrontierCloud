package node

import (
	"context"
	"encoding/json"
	"math"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestObservedResourceSyncRequiresActualMatchingFacts(t *testing.T) {
	desired := map[string]any{"enabled": true, "allocated_bytes": int64(42)}
	for _, tc := range []struct {
		name   string
		value  map[string]any
		online bool
		want   string
	}{
		{"offline", desired, false, "offline"},
		{"no heartbeat", nil, true, "awaiting"},
		{"missing capacity", map[string]any{"enabled": true}, true, "syncing"},
		{"wrong capacity", map[string]any{"enabled": true, "allocated_bytes": 41}, true, "syncing"},
		{"invalid capacity", map[string]any{"enabled": true, "allocated_bytes": "42"}, true, "syncing"},
		{"disabled", map[string]any{"enabled": false, "allocated_bytes": 42}, true, "syncing"},
		{"wire integers", map[string]any{"enabled": json.Number("1"), "allocated_bytes": json.Number("42")}, true, "effective"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := resourceSync(desired, tc.value, []string{"enabled", "allocated_bytes"}, tc.online); got != tc.want {
				t.Fatalf("%s want %s", got, tc.want)
			}
		})
	}
}

func TestObservedBackupHealthPrecedenceBoundsAndFallback(t *testing.T) {
	now := int64(200000)
	for _, tc := range []struct {
		enabled              bool
		last                 int64
		state, attempt, want string
	}{
		{false, 0, "receiving", "failed", "disabled"},
		{true, 0, "receiving", "failed", "running"},
		{true, now, "ready", "failed", "failed"},
		{true, 0, "waiting", "", "waiting-first-backup"},
		{true, now - 30*3600, "ready", "", "healthy"},
		{true, now - 30*3600 - 1, "ready", "", "stale"},
		{true, math.MaxInt64, "ready", "", "healthy"},
	} {
		observed := map[string]any{"state": tc.state, "last_success": tc.last, "generation": 7, "checksum": strings.Repeat("a", 80), "last_size_bytes": -1}
		if tc.attempt != "" {
			observed["last_attempt_state"] = tc.attempt
		}
		got := backupView(map[string]any{"enabled": tc.enabled}, observed, now)
		if got["health"] != tc.want || got["generation"] != int64(7) || len(got["checksum"].(string)) != 64 || got["last_size_bytes"] != int64(0) {
			t.Fatal(got)
		}
		if tc.last == math.MaxInt64 && (got["next_due"] != int64(math.MaxInt64) || got["lag_seconds"] != int64(0)) {
			t.Fatal("overflow", got)
		}
	}
}

func TestObservabilityProjectsHeartbeatWithoutNetworkOrCredentials(t *testing.T) {
	ctx := context.Background()
	transport := &localControl{nodes: map[string]*Service{}}
	m, md := testNode(t, "Master", "https://master.test", transport)
	f, fd := testNode(t, "Follower", "https://follower.test", transport)
	m.ConfigureVolumes(md.Pool(), &testVolume{}, recordingRoot(t))
	f.ConfigureVolumes(fd.Pool(), &testVolume{}, recordingRoot(t))
	transport.nodes["https://master.test"], transport.nodes["https://follower.test"] = m, f
	pack, err := f.CreatePair(ctx, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	id, err := m.ImportPair(ctx, pack, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	r, err := md.Nodes().Relationship(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled, cfg.Storage.Allocation = true, 2*store.GiB
	if err = md.Pool().ConfigureMember(ctx, r.PeerID, cfg, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err = m.Tick(ctx, r); err != nil {
		t.Fatal(err)
	}
	calls := transport.calls
	view, err := m.Observability(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if transport.calls != calls {
		t.Fatal("read view probed network")
	}
	members := view["members"].([]map[string]any)
	if len(members) != 1 {
		t.Fatal(view)
	}
	v := members[0]
	if v["member_id"] != r.PeerID || v["sync"].(map[string]any)["storage"] != "effective" || v["backup"].(map[string]any)["health"] != "disabled" {
		t.Fatal(v)
	}
	r, _ = md.Nodes().Relationship(ctx, id)
	credential, err := m.identity.vault.Unseal(r.Credential)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(view)
	for _, secret := range []string{credential, r.Credential, r.PublicKey} {
		if strings.Contains(string(b), secret) {
			t.Fatal("secret in projection")
		}
	}
	delete(transport.nodes, "https://follower.test")
	if err = m.Tick(ctx, r); err == nil {
		t.Fatal("expected offline probe")
	}
	view, err = m.Observability(ctx)
	if err != nil {
		t.Fatal(err)
	}
	v = view["members"].([]map[string]any)[0]
	if v["sync"].(map[string]any)["storage"] != "offline" || v["connection"].(map[string]any)["failures"] != 1 {
		t.Fatal(v)
	}
	if err = md.Nodes().RevokeRelationship(ctx, id, true, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	view, err = m.Observability(ctx)
	if err != nil {
		t.Fatal(err)
	}
	for _, member := range view["members"].([]map[string]any) {
		if member["connection"].(map[string]any)["status"] == "online" {
			t.Fatal("revoked relationship still online", member)
		}
	}
	// Fresh SQL role, not the startup identity, controls the projection.
	if _, err = md.Database().Exec("UPDATE node_identity SET role='Standalone' WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	view, err = m.Observability(ctx)
	if err != nil || view["role"] != "Standalone" || len(view["members"].([]map[string]any)) != 0 {
		t.Fatal(view, err)
	}
}
