package updater

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
)

func preparedHandoff(t *testing.T) (*DockerExecutor, *engineContract, *privateStore, Status, string) {
	t.Helper()
	x, f, status, target := executorFixture(t, "")
	s, err := x.private()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
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
	if out, err := x.Execute(context.Background(), Request{Target: target, Mode: "upgrade"}, status, progress); err != nil || !out.Handoff {
		t.Fatal(out, err)
	}
	x.Runtime = target
	return x, f, s, status, target
}

func TestHandoffMutationFailureRestoresControlOnlyAndKeepsMaintenance(t *testing.T) {
	for _, fault := range []string{"updater-create", "updater-create-lost", "updater-start", "updater-remove-lost", "updater-create-cancel"} {
		t.Run(fault, func(t *testing.T) {
			x, f, s, status, target := preparedHandoff(t)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			f.mu.Lock()
			f.fault = fault
			f.cancelMutation = cancel
			f.mu.Unlock()
			if err := x.Handoff(ctx); err == nil {
				t.Fatal("injected mutation failure hidden")
			}
			var failed Status
			if err := s.read("status.json", 8192, &failed); err != nil {
				t.Fatal(err)
			}
			if failed.State != "failed" || failed.CurrentSHA != target || failed.RuntimeSHA != f.old || failed.PreviousSHA != status.PreviousSHA {
				t.Fatal("wrong generation commit boundary", failed)
			}
			f.mu.Lock()
			agent, exists := f.container("private-updater")
			web, _ := f.container("private-web")
			agentRevision := f.images[agent.Image].Config.Labels["frontiercloud.revision"]
			webRevision := f.images[web.Image].Config.Labels["frontiercloud.revision"]
			f.mu.Unlock()
			if !exists || agent.State.Status != "running" || agentRevision != f.old || webRevision != target {
				t.Fatal("control lost or committed Web rolled back", agent, web)
			}
			x.Runtime = f.old
			maintenance := filepath.Join(t.TempDir(), "maintenance")
			d, err := NewDaemon(context.Background(), x.ControlDirectory, maintenance, f.old, "gin_main", x)
			if err != nil {
				t.Fatal(err)
			}
			defer d.Close()
			if d.Status().State != "failed" || d.Status().CurrentSHA != target {
				t.Fatal("fallback falsely reported success", d.Status())
			}
			if _, err = os.Stat(filepath.Join(maintenance, "enabled")); err != nil {
				t.Fatal("fallback reopened maintenance", err)
			}
			var h handoff
			if err = s.read("handoff.json", 16<<20, &h); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("acknowledged fallback journal retained", err)
			}
		})
	}
}

func TestHandoffLostStartReplyKeepsRunningTarget(t *testing.T) {
	x, f, s, status, target := preparedHandoff(t)
	f.mu.Lock()
	f.fault = "updater-start-lost"
	f.mu.Unlock()
	if err := x.Handoff(context.Background()); err != nil {
		t.Fatal("lost successful start treated as failure", err)
	}
	f.mu.Lock()
	c, _ := f.container("private-updater")
	revision := f.images[c.Image].Config.Labels["frontiercloud.revision"]
	f.mu.Unlock()
	if revision != target || c.State.Status != "running" {
		t.Fatal(c)
	}
	var current Status
	if err := s.read("status.json", 8192, &current); err != nil || current.State != "restarting" {
		t.Fatal(current, err)
	}
	if err := x.Recover(context.Background(), status); err != nil {
		t.Fatal(err)
	}
}

func TestHandoffRequiresExclusiveLeaseAndValidatedOriginalProvenance(t *testing.T) {
	x, f, s, _, _ := preparedHandoff(t)
	done, err := s.handoffLease()
	if err != nil {
		t.Fatal(err)
	}
	if err = x.Handoff(context.Background()); !errors.Is(err, filelease.ErrBusy) {
		t.Fatal("concurrent helper admitted", err)
	}
	done()
	var h handoff
	if err = s.read("handoff.json", 16<<20, &h); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	image := f.images[h.Snapshot.Image]
	image.Config.Labels["frontiercloud.runtime"] = "python"
	f.images[h.Snapshot.Image] = image
	before := len(f.removed)
	f.mu.Unlock()
	if err = x.Handoff(context.Background()); err == nil {
		t.Fatal("unverified fallback image admitted")
	}
	f.mu.Lock()
	after := len(f.removed)
	f.mu.Unlock()
	if before != after {
		t.Fatal("original updater stopped before fallback provenance proof")
	}
}

func TestHandoffRecoveryNeverRemovesUnrelatedHelper(t *testing.T) {
	x, f, s, status, _ := preparedHandoff(t)
	if err := x.Handoff(context.Background()); err != nil {
		t.Fatal(err)
	}
	var h handoff
	if err := s.read("handoff.json", 16<<20, &h); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	helper, _ := f.container(h.Helper)
	helper.Config["Labels"].(map[string]any)["frontiercloud.project"] = "unrelated"
	f.containers[helper.ID] = helper
	f.mu.Unlock()
	if err := x.Recover(context.Background(), status); err == nil {
		t.Fatal("unrelated helper retired")
	}
	f.mu.Lock()
	_, exists := f.container(h.Helper)
	f.mu.Unlock()
	if !exists {
		t.Fatal("unrelated container removed")
	}
	if err := s.read("handoff.json", 16<<20, &h); err != nil {
		t.Fatal("unresolved journal removed", err)
	}
}
