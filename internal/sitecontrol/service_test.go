package sitecontrol

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
)

type testAgent struct {
	mu          sync.Mutex
	state       string
	unavailable bool
}

func (a *testAgent) Request(context.Context, map[string]any) (map[string]any, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.unavailable {
		return nil, errors.New("private error")
	}
	return map[string]any{"ok": true, "status": map[string]any{"state": a.state, "phase": "test"}}, nil
}
func TestSiteFlagsPreserveContractAndCannotReopenBusyOrOfflineRuntime(t *testing.T) {
	root := t.TempDir()
	a := &testAgent{state: "idle"}
	s, err := Open(root, a)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx := context.Background()
	for _, change := range []bool{true, true, false, false} {
		v, err := s.Set(ctx, change)
		if err != nil || v["maintenance"] != change || v["manual"] != change {
			t.Fatal(v, err)
		}
	}
	if _, err = s.Set(ctx, true); err != nil {
		t.Fatal(err)
	}
	for _, state := range []string{"queued", "running", "distributing", "restarting"} {
		a.state = state
		if _, err = s.Set(ctx, false); err == nil {
			t.Fatal("busy release reopened", state)
		}
		if exists, err := s.flag(Manual, "manual\n"); err != nil || !exists {
			t.Fatal("busy rejection erased manual flag", err)
		}
	}
	a.state = "failed"
	v, err := s.Set(ctx, false)
	if err != nil || v["maintenance"] != false || v["force_open"] != true || v["source"] != "manual-open-override" {
		t.Fatal(v, err)
	}
	gate, err := maintenance.Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	if err = gate.Enter(ctx, func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
	v, err = s.Status(ctx)
	if err != nil || v["source"] != "native-offline" || v["maintenance"] != true {
		t.Fatal("force-open defeated hard fence", v, err)
	}
	if _, err = s.Set(ctx, false); err == nil {
		t.Fatal("Admin removed offline fence")
	}
	if enabled, err := gate.Enabled(); err != nil || !enabled {
		t.Fatal(err)
	}
}
func TestSiteUnknownFlagsAndUpdaterOutageAreNotReopeningAuthority(t *testing.T) {
	root := t.TempDir()
	a := &testAgent{state: "idle", unavailable: true}
	s, err := Open(root, a)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx := context.Background()
	v, err := s.Set(ctx, true)
	if err != nil || v["maintenance"] != true {
		t.Fatal(v, err)
	}
	if _, err = s.Set(ctx, false); err == nil {
		t.Fatal("outage reopened site")
	}
	a.unavailable = false
	if err = os.WriteFile(filepath.Join(root, ForceOpen), []byte("unknown\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Set(ctx, true); !errors.Is(err, ErrState) {
		t.Fatal("unknown flag silently erased", err)
	}
	raw, _ := os.ReadFile(filepath.Join(root, ForceOpen))
	if string(raw) != "unknown\n" {
		t.Fatal("changed unknown flag")
	}
	os.Remove(filepath.Join(root, ForceOpen))
	foreign := filepath.Join(t.TempDir(), "foreign")
	os.WriteFile(foreign, []byte("untouched"), 0600)
	if err = os.Symlink(foreign, filepath.Join(root, ForceOpen)); err != nil {
		t.Skip("symlink unavailable")
	}
	if _, err = s.Set(ctx, false); !errors.Is(err, ErrState) {
		t.Fatal("followed flag symlink", err)
	}
	raw, _ = os.ReadFile(foreign)
	if string(raw) != "untouched" {
		t.Fatal("foreign bytes changed")
	}
}
func TestSiteSharedOperatorLeaseHonorsCancellation(t *testing.T) {
	root := t.TempDir()
	s, err := Open(root, &testAgent{state: "idle"})
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	held, err := Lease(context.Background(), s.root, true)
	if err != nil {
		t.Fatal(err)
	}
	defer held()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if _, err = s.Status(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("query bypassed operator lease", err)
	}
}
