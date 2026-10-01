package filelease

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestLeaseChild(t *testing.T) {
	name := os.Getenv("FRONTIERCLOUD_TEST_LEASE_FILE")
	if name == "" {
		return
	}
	f, err := os.OpenFile(name, os.O_RDWR|os.O_CREATE, 0600)
	if err != nil {
		t.Fatal(err)
	}
	release, err := Acquire(context.Background(), f, true)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	fmt.Println("LEASE_ACQUIRED")
	// The parent terminates this subprocess to exercise crash release.
	select {}
}
func TestLeaseExcludesOtherProcessesAndReleasesAfterCrash(t *testing.T) {
	name := filepath.Join(t.TempDir(), "mutation.lock")
	command := exec.Command(os.Args[0], "-test.run=^TestLeaseChild$", "-test.timeout=30s")
	command.Env = append(os.Environ(), "FRONTIERCLOUD_TEST_LEASE_FILE="+name)
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
			if scanner.Text() == "LEASE_ACQUIRED" {
				ready <- true
				return
			}
		}
		ready <- false
	}()
	select {
	case ok := <-ready:
		if !ok {
			t.Fatal("child did not acquire lease")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("child lease timeout")
	}
	f, err := os.OpenFile(name, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if release, err := Acquire(ctx, f, false); !errors.Is(err, context.DeadlineExceeded) {
		if release != nil {
			release()
		}
		t.Fatalf("cross-process shared lease bypassed writer: %v", err)
	}
	if err := command.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	command.Wait()
	f, err = os.OpenFile(name, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	release, err := Acquire(context.Background(), f, true)
	if err != nil {
		t.Fatal("crashed process retained lease", err)
	}
	release()
	release()
}
