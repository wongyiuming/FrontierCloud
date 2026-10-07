package node

import (
	"context"
	"reflect"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type capabilityControl struct {
	*localControl
	malformed bool
}

func (l *capabilityControl) Request(ctx context.Context, origin, route, method string, value any, relationship, credential string) (map[string]any, error) {
	result, err := l.localControl.Request(ctx, origin, route, method, value, relationship, credential)
	if err == nil && route == "/internal/v1/heartbeat" {
		if l.malformed {
			result["capabilities"] = []any{true}
		} else {
			delete(result, "capabilities")
		}
	}
	return result, err
}

func TestCapabilitiesLegacyHeartbeatAndInvalidFactsDoNotGrantReadiness(t *testing.T) {
	ctx := context.Background()
	transport := &capabilityControl{localControl: &localControl{nodes: map[string]*Service{}}}
	m, md := testNode(t, "Master", "https://master.test", transport)
	f, fd := testNode(t, "Follower", "https://follower.test", transport)
	m.ConfigureVolumes(md.Pool(), &testVolume{}, recordingRoot(t))
	f.ConfigureVolumes(fd.Pool(), &testVolume{}, recordingRoot(t))
	transport.nodes["https://master.test"], transport.nodes["https://follower.test"] = m, f
	envelope, err := m.SignedIdentity(ctx, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
	if err != nil {
		t.Fatal(err)
	}
	if err = protocol.Verify(envelope.Payload["public_key"].(string), envelope.Payload, envelope.Signature); err != nil {
		t.Fatal(err)
	}
	features, err := protocol.ReadCapabilities(envelope.Payload)
	if err != nil || !reflect.DeepEqual(features, protocol.BaselineCapabilities()) {
		t.Fatal(features, err)
	}
	pack, err := f.CreatePair(ctx, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	id, err := m.ImportPair(ctx, pack, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	mr, err := md.Nodes().Relationship(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if err = m.Tick(ctx, mr); err != nil {
		t.Fatal("legacy v2 peer rejected", err)
	}
	mr, err = md.Nodes().Relationship(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	features, err = protocol.ReadCapabilities(mr.Summary)
	if err != nil || !reflect.DeepEqual(features, protocol.BaselineCapabilities()) || mr.Status != "online" {
		t.Fatal(mr, features, err)
	}
	if _, err = protocol.NegotiateCapabilities(mr.Summary, "release-manifest-v1"); err == nil {
		t.Fatal("new release feature inferred for legacy peer")
	}
	fr, err := fd.Nodes().Relationship(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	before, err := fd.Pool().FollowerSummary(ctx, 10*store.GiB, 20*store.GiB)
	if err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled = true
	cfg.Storage.Allocation = 2 * store.GiB
	if _, err = f.ReceiveHeartbeat(ctx, fr, map[string]any{"capabilities": []any{true}, "mode": "Direct", "resources": resourceWire(cfg)}); err == nil {
		t.Fatal("invalid capability applied")
	}
	frAfter, err := fd.Nodes().Relationship(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	after, err := fd.Pool().FollowerSummary(ctx, 10*store.GiB, 20*store.GiB)
	if err != nil {
		t.Fatal(err)
	}
	if frAfter.Mode != fr.Mode || !reflect.DeepEqual(before, after) {
		t.Fatal("invalid capability changed desired configuration", frAfter, after)
	}
	transport.malformed = true
	if err = m.Tick(ctx, mr); err == nil {
		t.Fatal("malformed peer observation accepted")
	}
	mr, err = md.Nodes().Relationship(ctx, id)
	if err != nil || mr.Status != "degraded" || mr.Failures != 1 {
		t.Fatal(mr, err)
	}
}
