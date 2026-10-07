package updater

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/release"
)

const testCurrent = "1111111111111111111111111111111111111111"
const testTarget = "2222222222222222222222222222222222222222"

func TestRecoveryFailureClassificationIsBounded(t *testing.T) {
	for _, test := range []struct {
		err  error
		want string
	}{
		{&recoveryFailure{stage: "handoff-helper", cause: &dockerFailure{operation: "container-inspect", status: 500}}, "recovery-handoff-helper/docker-container-inspect-500"},
		{&recoveryFailure{stage: "web-health", cause: ErrState}, "recovery-web-health/state-proof"},
		{&recoveryFailure{stage: "private-store", cause: errors.New("password=private credential")}, "recovery-private-store/operation"},
		{&recoveryFailure{stage: "password=private credential", cause: ErrState}, "recovery-unknown/state-proof"},
		{&recoveryFailure{stage: "journals", cause: &recoveryFailure{stage: "password=private credential", cause: ErrState}}, "recovery-journals/operation"},
	} {
		if got := safeFailureCode(test.err); got != test.want {
			t.Fatal("unsafe or imprecise recovery classification", got)
		}
	}
}

type testExecutor struct {
	entered    chan struct{}
	wait       chan struct{}
	fail       bool
	recoveries int
	mu         sync.Mutex
}

func (e *testExecutor) Execute(ctx context.Context, r Request, s Status, progress func(Checkpoint) error) (Outcome, error) {
	// Queued must be durable before the worker executes.
	if err := progress(Checkpoint{State: "running", Phase: "building"}); err != nil {
		return Outcome{}, err
	}
	if e.entered != nil {
		close(e.entered)
	}
	if e.wait != nil {
		select {
		case <-e.wait:
		case <-ctx.Done():
			return Outcome{}, ctx.Err()
		}
	}
	if e.fail {
		return Outcome{}, errors.New("secret credentials must not appear in status")
	}
	return Outcome{Current: r.Target, Previous: s.CurrentSHA}, nil
}
func (e *testExecutor) Recover(context.Context, Status) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.recoveries++
	return nil
}
func daemonFixture(t *testing.T, e Executor) (*Daemon, string, string) {
	t.Helper()
	directory, err := os.MkdirTemp("", "fc-upd-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(directory) })
	control := filepath.Join(directory, "c")
	flags := filepath.Join(directory, "m")
	d, err := NewDaemon(context.Background(), control, flags, testCurrent, "gin_main", e)
	if err != nil {
		t.Fatal(err)
	}
	return d, control, flags
}
func serveTest(t *testing.T, d *Daemon, control string) (context.CancelFunc, <-chan error) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- d.Serve(ctx) }()
	for end := time.Now().Add(3 * time.Second); time.Now().Before(end); {
		if _, err := os.Lstat(filepath.Join(control, "control.sock")); err == nil {
			return cancel, done
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	t.Fatal("socket did not open")
	return nil, nil
}
func TestDaemonDurableConcurrentStartAndInterruptedRecovery(t *testing.T) {
	e := &testExecutor{entered: make(chan struct{}), wait: make(chan struct{})}
	d, control, flags := daemonFixture(t, e)
	cancel, done := serveTest(t, d, control)
	var wg sync.WaitGroup
	var mu sync.Mutex
	accepted := 0
	for range 20 {
		wg.Go(func() {
			out, err := d.Start(Request{Target: testTarget, Mode: "upgrade", Hold: true})
			if err != nil {
				t.Error(err)
				return
			}
			if out["ok"] == true {
				mu.Lock()
				accepted++
				mu.Unlock()
			}
		})
	}
	wg.Wait()
	if accepted != 1 {
		t.Fatalf("accepted %d", accepted)
	}
	<-e.entered
	var persisted Status
	if err := d.store.read("status.json", 8192, &persisted); err != nil || persisted.State != "running" || persisted.TargetSHA != testTarget || !persisted.HoldMaintenance {
		t.Fatalf("not durable: %+v %v", persisted, err)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if d.Status().State != "failed" {
		t.Fatal(d.Status())
	}
	if strings.Contains(d.Status().Detail, "secret") {
		t.Fatal("leaked executor error")
	}
	if d.Status().Detail != "release failed (building/operation); maintenance retained" {
		t.Fatal("failure stage must remain bounded and credential-free", d.Status().Detail)
	}
	d.Close()
	if _, err := os.Stat(filepath.Join(flags, "enabled")); err != nil {
		t.Fatal("lost failure fence", err)
	}
	// Simulate process death after durable queue publication, before a worker.
	d, err := NewDaemon(context.Background(), control, flags, testCurrent, "gin_main", e)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = d.Start(Request{Target: testTarget, Mode: "rollback"}); err != nil {
		t.Fatal(err)
	}
	d.Close()
	d, err = NewDaemon(context.Background(), control, flags, testCurrent, "gin_main", e)
	if err != nil {
		t.Fatal(err)
	}
	defer d.Close()
	if d.Status().State != "failed" || d.Status().Phase != "interrupted" {
		t.Fatal(d.Status())
	}
	e.mu.Lock()
	count := e.recoveries
	e.mu.Unlock()
	if count != 2 {
		t.Fatalf("recovery count %d", count)
	}
}
func TestDaemonSocketStrictBoundsAndNativeAgent(t *testing.T) {
	d, control, _ := daemonFixture(t, &testExecutor{})
	defer d.Close()
	cancel, done := serveTest(t, d, control)
	defer func() {
		cancel()
		if err := <-done; err != nil {
			t.Error(err)
		}
	}()
	agent := release.SocketAgent{Path: filepath.Join(control, "control.sock")}
	out, err := agent.Request(context.Background(), map[string]any{"action": "status"})
	if err != nil || out["ok"] != true {
		t.Fatalf("%v %v", out, err)
	}
	for _, raw := range []string{`{"action":"status","action":"start"}` + "\n", `{"action":"status"} {}` + "\n", `{"action":"start","target_sha":"` + testTarget + `","mode":"upgrade","hold_maintenance":"false"}` + "\n", `{"action":"status","secret":"x"}` + "\n", strings.Repeat("x", 8192) + "\n"} {
		conn, err := net.Dial("unix", agent.Path)
		if err != nil {
			t.Fatal(err)
		}
		conn.SetDeadline(time.Now().Add(time.Second))
		conn.Write([]byte(raw))
		var value map[string]any
		err = json.NewDecoder(conn).Decode(&value)
		conn.Close()
		if err != nil || value["ok"] != false {
			t.Fatalf("accepted invalid request: %v %v", value, err)
		}
	}
	if d.Status().State != "idle" {
		t.Fatal("invalid request queued work")
	}
}
func TestPrivateStoreSingleOwnerCorruptionAndSymlinkFence(t *testing.T) {
	d, control, flags := daemonFixture(t, &testExecutor{})
	if other, err := NewDaemon(context.Background(), control, flags, testCurrent, "gin_main", &testExecutor{}); !errors.Is(err, filelease.ErrBusy) {
		if other != nil {
			other.Close()
		}
		t.Fatalf("second owner: %v", err)
	}
	d.Close()
	if err := os.WriteFile(filepath.Join(control, "status.json"), []byte(`{"state":"idle","state":"success"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if other, err := NewDaemon(context.Background(), control, flags, testCurrent, "gin_main", &testExecutor{}); err == nil {
		other.Close()
		t.Fatal("corrupt status admitted")
	}
	if _, err := os.Stat(filepath.Join(flags, "enabled")); err != nil {
		t.Fatal("corruption not fenced")
	}
	os.Remove(filepath.Join(control, "status.json"))
	target := filepath.Join(t.TempDir(), "foreign")
	os.WriteFile(target, []byte("untouched"), 0600)
	if err := os.Symlink(target, filepath.Join(control, "status.json")); err != nil {
		t.Skip("symlink unavailable")
	}
	s, err := openPrivate(control, true)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if err = s.write("status.json", Status{}, 8192); !errors.Is(err, ErrState) {
		t.Fatal("followed status symlink", err)
	}
	raw, _ := os.ReadFile(target)
	if string(raw) != "untouched" {
		t.Fatal("foreign file changed")
	}
}
func TestDaemonDoesNotClaimSuccessWithoutCompiledRuntimeHandoff(t *testing.T) {
	d, _, flags := daemonFixture(t, &testExecutor{})
	defer d.Close()
	if _, err := d.Start(Request{Target: testTarget, Mode: "upgrade"}); err != nil {
		t.Fatal(err)
	}
	request := <-d.queue
	d.perform(context.Background(), request)
	if d.Status().State != "failed" || d.Status().Detail != "updater runtime handoff missing" {
		t.Fatal(d.Status())
	}
	if _, err := os.Stat(filepath.Join(flags, "enabled")); err != nil {
		t.Fatal(err)
	}
}
