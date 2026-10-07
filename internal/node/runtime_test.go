package node

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type testVolume struct{ occupied bool }

func (v *testVolume) ControlLease(context.Context) (func(), error) { return func() {}, nil }

func (v *testVolume) WithPromotion(ctx context.Context, role string, apply func(store.NodePromotion) error) error {
	if v.occupied && role == "Follower" {
		return errors.New("occupied business volume")
	}
	return apply(store.NodePromotion{Role: role, PhysicalFree: 10 * store.GiB})
}
func (v *testVolume) PhysicalCapacity(context.Context) (int64, int64, error) {
	return 20 * store.GiB, 10 * store.GiB, nil
}
func recordingRoot(t *testing.T) *os.Root {
	t.Helper()
	root, err := os.OpenRoot(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { root.Close() })
	return root
}

func TestOutboundHeartbeatAloneEstablishesReachabilityAndDesiredMode(t *testing.T) {
	ctx := context.Background()
	transport := &localControl{nodes: map[string]*Service{}}
	master, md := testNode(t, "Master", "https://master.test", transport)
	follower, fd := testNode(t, "Follower", "https://follower.test", transport)
	master.ConfigureVolumes(md.Pool(), &testVolume{}, recordingRoot(t))
	follower.ConfigureVolumes(fd.Pool(), &testVolume{}, recordingRoot(t))
	transport.nodes["https://master.test"] = master
	transport.nodes["https://follower.test"] = follower
	packageValue, err := follower.CreatePair(ctx, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	id, err := master.ImportPair(ctx, packageValue, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	mr, _ := master.repo.Relationship(ctx, id)
	fr, _ := follower.repo.Relationship(ctx, id)
	config := store.ResourceConfiguration{}
	config.Storage.Enabled = true
	config.Storage.Allocation = 2 * store.GiB
	if err = md.Pool().ConfigureMember(ctx, mr.PeerID, config, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err = md.Nodes().SetRelationshipMode(ctx, id, "Direct", false, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	mr, _ = md.Nodes().Relationship(ctx, id)
	if _, err = follower.ReceiveHeartbeat(ctx, fr, map[string]any{"mode": "Direct", "resources": resourceWire(config)}); err != nil {
		t.Fatal(err)
	}
	fr, _ = fd.Nodes().Relationship(ctx, id)
	if fr.Status != "offline" || fr.LastHeartbeat != 0 || fr.Mode != "Direct" {
		t.Fatalf("inbound probe hid unreachable upstream %+v", fr)
	}
	if err = master.Tick(ctx, mr); err != nil {
		t.Fatal(err)
	}
	mr, _ = md.Nodes().Relationship(ctx, id)
	fr, _ = fd.Nodes().Relationship(ctx, id)
	if mr.Status != "online" || mr.LastHeartbeat == 0 || fr.Status != "offline" || fr.LastHeartbeat != 0 {
		t.Fatalf("outbound/inbound distinction %+v %+v", mr, fr)
	}
	members, err := md.Pool().Members(ctx)
	if err != nil {
		t.Fatal(err)
	}
	for _, member := range members {
		if member.ID == mr.PeerID && (member.Available != 2*store.GiB || member.Transport != "Direct") {
			t.Fatalf("pool did not converge %+v", member)
		}
	}
	if err = follower.Tick(ctx, fr); err != nil {
		t.Fatal(err)
	}
	fr, _ = fd.Nodes().Relationship(ctx, id)
	if fr.Status != "online" || fr.LastHeartbeat == 0 {
		t.Fatal("Follower outbound probe", fr)
	}
	delete(transport.nodes, "https://follower.test")
	if err = master.Tick(ctx, mr); err == nil {
		t.Fatal("failed outgoing request marked healthy")
	}
	mr, _ = md.Nodes().Relationship(ctx, id)
	if mr.Status != "degraded" || mr.Failures != 1 {
		t.Fatalf("lost failure %+v", mr)
	}
	transport.nodes["https://follower.test"] = follower
	if err = master.Tick(ctx, mr); err != nil {
		t.Fatal(err)
	}
	mr, _ = md.Nodes().Relationship(ctx, id)
	if mr.Status != "online" || mr.Recoveries != 1 {
		t.Fatalf("lost recovery %+v", mr)
	}
	if _, err = master.ReceiveHeartbeat(ctx, mr, map[string]any{"mode": "Relay"}); err == nil {
		t.Fatal("Follower selected Master transport")
	}
}
func TestPromotionServiceRequiresPinnedSelfIdentityAndEmptyVolumes(t *testing.T) {
	ctx := context.Background()
	transport := &localControl{nodes: map[string]*Service{}}
	standalone, db := testNode(t, "Standalone", "https://node.test", transport)
	transport.nodes["https://node.test"] = standalone
	volume := &testVolume{occupied: true}
	standalone.ConfigureVolumes(db.Pool(), volume, recordingRoot(t))
	if _, err := standalone.Promote(ctx, "Follower", "https://node.test", 0, store.NodeAudit{}); err == nil {
		t.Fatal("occupied Follower promoted")
	}
	volume.occupied = false
	other, _ := testNode(t, "Standalone", "https://other.test", transport)
	transport.nodes["https://other.test"] = other
	if _, err := standalone.Promote(ctx, "Follower", "https://other.test", 0, store.NodeAudit{}); err == nil {
		t.Fatal("unrelated endpoint accepted")
	}
	file, err := standalone.recordings.Create("recording.bin")
	if err != nil {
		t.Fatal(err)
	}
	file.Close()
	if _, err = standalone.Promote(ctx, "Follower", "https://node.test", 0, store.NodeAudit{}); err == nil {
		t.Fatal("existing recording orphaned")
	}
	if err = standalone.recordings.Remove("recording.bin"); err != nil {
		t.Fatal(err)
	}
	row, err := standalone.Promote(ctx, "Follower", "https://node.test", 0, store.NodeAudit{})
	if err != nil || row.Role != "Follower" {
		t.Fatal("promotion", row, err)
	}
	if _, err = standalone.Promote(ctx, "Master", "https://node.test", store.GiB, store.NodeAudit{}); err == nil {
		t.Fatal("fixed role changed")
	}
	packageValue, err := standalone.CreatePair(ctx, store.NodeAudit{})
	if err != nil || packageValue.Payload["node_id"] != row.ID {
		t.Fatal("promotion did not refresh role", err)
	}
}
func TestHeartbeatWindowBoundsAndPythonRounding(t *testing.T) {
	now := time.Now().Unix()
	previous := map[string]any{"heartbeat": map[string]any{"samples": []any{[]any{json.Number("1"), json.Number("99")}, []any{json.Number("9999999999"), json.Number("999")}, []any{now, 1}, []any{now, -1}, "bad"}}}
	window := heartbeatWindow(previous, 2, now)
	if window["count"] != 2 || window["avg_ms"] != int64(2) || window["min_ms"] != int64(1) || window["max_ms"] != int64(2) {
		t.Fatalf("window %+v", window)
	}
	previous["heartbeat"] = map[string]any{"samples": []any{[]any{now, 2}}}
	window = heartbeatWindow(previous, 3, now)
	if window["avg_ms"] != int64(2) {
		t.Fatal("not Python ties-to-even rounding")
	}
	values := []any{}
	for range 300 {
		values = append(values, []any{now, 3})
	}
	previous["heartbeat"] = map[string]any{"samples": values}
	if got := heartbeatWindow(previous, 4, now)["count"]; got != 180 {
		t.Fatal("unbounded window", got)
	}
}
