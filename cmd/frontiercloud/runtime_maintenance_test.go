package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"syscall"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
)

func TestNativeServerMaintenanceProcess(t *testing.T) {
	if os.Getenv("FRONTIERCLOUD_TEST_NATIVE_SERVER") == "1" {
		if err := serve(); err != nil {
			t.Fatal(err)
		}
		return
	}
	redisURL := os.Getenv("FRONTIERCLOUD_TEST_REDIS_URL")
	if redisURL == "" || runtime.GOOS == "windows" {
		t.Skip("requires isolated Redis and Unix signal handling")
	}
	directory := t.TempDir()
	data := filepath.Join(directory, "data")
	secrets := filepath.Join(directory, "secrets")
	for _, path := range []string{filepath.Join(data, "media", "music"), filepath.Join(data, "media", "vido"), filepath.Join(data, "media", "lyrics"), filepath.Join(data, "recordings"), secrets} {
		if err := os.MkdirAll(path, 0700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(secrets, "admin_key"), []byte("isolated-test-admin-key"), 0600); err != nil {
		t.Fatal(err)
	}
	static, err := filepath.Abs("../../static")
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	for key, value := range map[string]string{
		"DB_TYPE": "sqlite", "SQLITE_PATH": filepath.Join(directory, "node.sqlite"),
		"DATA_ROOT": data, "STATIC_ROOT": static, "SECRETS_DIR": secrets,
		"HTTP_ADDR": address, "REDIS_URL": redisURL, "TLS_ENABLED": "false",
		"FRONTIERCLOUD_TEST_NATIVE_SERVER": "1",
	} {
		t.Setenv(key, value)
	}
	start := func() <-chan error {
		t.Helper()
		cmd := exec.Command(os.Args[0], "-test.run=^TestNativeServerMaintenanceProcess$", "-test.count=1")
		var output bytes.Buffer
		cmd.Stdout, cmd.Stderr = &output, &output
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		done := make(chan error, 1)
		go func() { done <- cmd.Wait(); close(done) }()
		t.Cleanup(func() { cmd.Process.Kill(); <-done })
		client := &http.Client{Timeout: 250 * time.Millisecond}
		deadline := time.Now().Add(15 * time.Second)
		for time.Now().Before(deadline) {
			select {
			case err := <-done:
				t.Fatalf("native process exited before readiness: %v\n%s", err, output.String())
			default:
			}
			response, err := client.Get("http://" + address + "/health/ready")
			if err == nil {
				io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
				response.Body.Close()
				if response.StatusCode == http.StatusOK {
					return done
				}
			}
			time.Sleep(25 * time.Millisecond)
		}
		cmd.Process.Signal(syscall.SIGTERM)
		select {
		case <-done:
			t.Fatalf("readiness timeout\n%s", output.String())
		case <-time.After(2 * time.Second):
			t.Fatal("readiness timeout and shutdown did not complete")
		}
		return nil
	}
	gate, err := maintenance.Open(data)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	settings, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	check := func(ctx context.Context) error {
		db, err := openExistingStore(ctx, settings)
		if err != nil {
			return err
		}
		defer db.Close()
		snapshot, err := db.Maintenance().InspectMaintenance(ctx)
		if err == nil && (snapshot.Role != "Standalone" || !snapshot.LogicalIdle) {
			return errors.New("process did not leave an idle Standalone fixture")
		}
		return err
	}
	for iteration := 0; iteration < 2; iteration++ {
		done := start()
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		err := gate.Enter(ctx, check)
		cancel()
		if err != nil {
			t.Fatal("could not drain real native server", err)
		}
		select {
		case err := <-done:
			if err != nil {
				t.Fatal("native server shutdown failed", err)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("process retained work after its runtime lease released")
		}
		if err := serve(); !errors.Is(err, maintenance.ErrEnabled) {
			t.Fatal("native startup bypassed persistent maintenance", err)
		}
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		err = gate.Resume(ctx, check)
		cancel()
		if err != nil {
			t.Fatal(err)
		}
	}
}
