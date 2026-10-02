package maintenance

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func fixture(t *testing.T) (*Gate, string) {
	t.Helper()
	directory := t.TempDir()
	gate, err := Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { gate.Close() })
	return gate, directory
}
func TestNativeMaintenancePersistsBeforeDrainAndHoldsLeaseThroughHTTP(t *testing.T) {
	gate, directory := fixture(t)
	runtime, err := gate.Runtime(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer runtime.Close()
	entered, finish := make(chan struct{}), make(chan struct{})
	requestDone := make(chan struct{})
	var cancelled bool
	handler := runtime.Handler(http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) {
		close(entered)
		<-q.Context().Done()
		cancelled = true
		// An in-flight handler may do bounded durable cleanup after cancellation.
		// Its ignored-context work must remain covered by the lifecycle lease.
		<-finish
	}))
	go func() {
		defer close(requestDone)
		handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("POST", "/upload", nil))
	}()
	<-entered
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	called := false
	if err := gate.Enter(ctx, func(context.Context) error { called = true; return nil }); !errors.Is(err, context.DeadlineExceeded) || called {
		t.Fatal("active runtime claimed drained", err, called)
	}
	if enabled, err := gate.Enabled(); err != nil || !enabled {
		t.Fatal("timeout reopened fence", enabled, err)
	}
	if _, err := gate.Runtime(context.Background()); !errors.Is(err, ErrEnabled) {
		t.Fatal("new runtime bypassed fence", err)
	}
	if err := os.WriteFile(filepath.Join(directory, ".frontiercloud-force-open"), []byte("override"), 0600); err != nil {
		t.Fatal(err)
	}
	result := httptest.NewRecorder()
	runtime.Handler(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Fatal("new HTTP request admitted") })).ServeHTTP(result, httptest.NewRequest("GET", "/health/live", nil))
	if result.Code != 503 || result.Header().Get("Cache-Control") != "no-store" || result.Header().Get("Retry-After") == "" {
		t.Fatal(result.Code, result.Body.String())
	}
	closing, closed := make(chan struct{}), make(chan struct{})
	go func() { close(closing); runtime.Close(); close(closed) }()
	<-closing
	short, stop := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer stop()
	if err := gate.Inspect(short, func(context.Context) error { t.Fatal("cleanup not joined"); return nil }); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	close(finish)
	<-requestDone
	<-closed
	if !cancelled {
		t.Fatal("handler context not cancelled")
	}
	if err := gate.Inspect(context.Background(), func(context.Context) error { return nil }); err != nil {
		t.Fatal("lease retained after full shutdown", err)
	}
	sentinel := errors.New("offline proof failed")
	if err := gate.Resume(context.Background(), func(context.Context) error { return sentinel }); !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
	if enabled, err := gate.Enabled(); err != nil || !enabled {
		t.Fatal("failed check reopened fence", err)
	}
	if err := gate.Resume(context.Background(), func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if enabled, err := gate.Enabled(); err != nil || enabled {
		t.Fatal("resume did not reopen", enabled, err)
	}
	next, err := gate.Runtime(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	next.Close()
}

func TestNativeMaintenanceUnsafeStateAndFailedCheckAreClosed(t *testing.T) {
	gate, directory := fixture(t)
	sentinel := errors.New("proof failed")
	if err := gate.Enter(context.Background(), func(context.Context) error { return sentinel }); !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
	if _, err := gate.Runtime(context.Background()); !errors.Is(err, ErrEnabled) {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, Marker), []byte("invalid"), 0600); err != nil {
		t.Fatal(err)
	}
	if enabled, err := gate.Enabled(); !enabled || !errors.Is(err, ErrState) {
		t.Fatal(enabled, err)
	}
	if err := gate.Resume(context.Background(), func(context.Context) error { t.Fatal("unsafe marker accepted"); return nil }); !errors.Is(err, ErrState) {
		t.Fatal(err)
	}
	other, dir := fixture(t)
	if err := os.Mkdir(filepath.Join(dir, runtimeLock), 0700); err != nil {
		t.Fatal(err)
	}
	if _, err := other.Runtime(context.Background()); !errors.Is(err, ErrState) {
		t.Fatal("directory used as lock", err)
	}
	link := filepath.Join(t.TempDir(), "link")
	if err := os.Symlink(directory, link); err != nil {
		t.Skip("symlink unavailable", err)
	}
	if _, err := Open(link); !errors.Is(err, ErrState) {
		t.Fatal("symlink root accepted", err)
	}
}

func TestMaintenanceRuntimeChild(t *testing.T) {
	directory := os.Getenv("FRONTIERCLOUD_TEST_MAINTENANCE_ROOT")
	if directory == "" {
		return
	}
	gate, err := Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	runtime, err := gate.Runtime(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer runtime.Close()
	fmt.Println("NATIVE_RUNTIME_ACQUIRED")
	// Simulate ignored-context shutdown work. Only process death can release it.
	<-runtime.Context().Done()
	select {}
}
func TestNativeMaintenanceCrossProcessCrashKeepsFence(t *testing.T) {
	gate, directory := fixture(t)
	command := exec.Command(os.Args[0], "-test.run=^TestMaintenanceRuntimeChild$", "-test.timeout=30s")
	command.Env = append(os.Environ(), "FRONTIERCLOUD_TEST_MAINTENANCE_ROOT="+directory)
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	command.Stderr = os.Stderr
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { command.Process.Kill(); command.Wait() })
	ready := make(chan bool, 1)
	go func() {
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			if scanner.Text() == "NATIVE_RUNTIME_ACQUIRED" {
				ready <- true
				return
			}
		}
		ready <- false
	}()
	select {
	case ok := <-ready:
		if !ok {
			t.Fatal("child failed")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("child timeout")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	if err := gate.Enter(ctx, func(context.Context) error { t.Fatal("live process bypassed"); return nil }); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	if err := command.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	command.Wait()
	if err := gate.Inspect(context.Background(), func(context.Context) error { return nil }); err != nil {
		t.Fatal("crashed process retained lease", err)
	}
	if _, err := gate.Runtime(context.Background()); !errors.Is(err, ErrEnabled) {
		t.Fatal("crash reopened marker", err)
	}
}
