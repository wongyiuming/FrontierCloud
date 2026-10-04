package node

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/deployment"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func admissionFixture(t *testing.T) (string, *Identity) {
	t.Helper()
	dir := t.TempDir()
	ctx := context.Background()
	c := config.Config{DataRoot: dir, DatabaseType: config.DatabaseSQLite, SQLitePath: filepath.Join(dir, "db.sqlite"), SecretsDirectory: filepath.Join(dir, "secrets")}
	if err := deployment.Bind(ctx, c, nil); err != nil {
		t.Fatal(err)
	}
	db, err := sqlitestore.Open(c.SQLitePath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := db.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	n, err := Initialize(ctx, db.Nodes(), c.SecretsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	return dir, n
}

func TestOfflineMasterAdmissionRequiresClosedFenceSuccessfulHeldProof(t *testing.T) {
	dir, n := admissionFixture(t)
	n.Role = "Master"
	ctx := context.Background()
	called, released := false, false
	proof := func(context.Context) (func(), error) {
		called = true
		return func() {
			released = true
			if err := n.CheckNativeRuntime(ctx, dir); err != nil {
				t.Error("write fence released before durable receipt", err)
			}
		}, nil
	}
	if err := n.AdmitVerifiedMaster(ctx, dir, proof); err == nil || called {
		t.Fatal("open gate granted migration authority")
	}
	g, err := maintenance.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer g.Close()
	if err := g.Enter(ctx, func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
	for _, verify := range []func(context.Context) (func(), error){nil, func(context.Context) (func(), error) { return nil, errors.New("mismatch") }, func(context.Context) (func(), error) { return nil, nil }} {
		if err := n.AdmitVerifiedMaster(ctx, dir, verify); err == nil {
			t.Fatal("unproven migration accepted")
		}
		if err := n.CheckNativeRuntime(ctx, dir); !errors.Is(err, ErrRuntimeAdmission) {
			t.Fatal("failed proof published receipt", err)
		}
	}
	if err := n.AdmitVerifiedMaster(ctx, dir, proof); err != nil || !called || !released {
		t.Fatal("verified offline admission failed", err)
	}
	if enabled, err := g.Enabled(); err != nil || !enabled {
		t.Fatal("admission reopened maintenance", err)
	}
	previous := n.ID
	n.ID = strings.Repeat("f", 32)
	if err := n.AdmitVerifiedMaster(ctx, dir, func(context.Context) (func(), error) { return func() {}, nil }); !errors.Is(err, ErrRuntimeAdmission) {
		t.Fatal("migration overwrote foreign identity receipt", err)
	}
	n.ID = previous
	n.Role = "Follower"
	if err := n.AdmitVerifiedMaster(ctx, dir, proof); !errors.Is(err, ErrRuntimeAdmission) {
		t.Fatal("Master migration admitted Follower", err)
	}
}

func TestNativeRuntimeReceiptRequiresCompletedStartupAndPreservesRoleFence(t *testing.T) {
	dir, n := admissionFixture(t)
	ctx := context.Background()
	if err := n.CheckNativeRuntime(ctx, dir); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, runtimeReceipt)); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("check published a receipt", err)
	}
	for _, role := range []string{"Master", "Follower"} {
		n.Role = role
		for _, check := range []func(context.Context, string) error{n.CheckNativeRuntime, n.RecordNativeRuntime} {
			if err := check(ctx, dir); !errors.Is(err, ErrRuntimeAdmission) {
				t.Fatal("unproven existing role admitted", role, err)
			}
		}
	}
	n.Role = "Standalone"
	if err := n.RecordNativeRuntime(ctx, dir); err != nil {
		t.Fatal(err)
	}
	proof, err := os.ReadFile(filepath.Join(dir, runtimeReceipt))
	if err != nil || strings.Contains(string(proof), n.ID) {
		t.Fatal("receipt leaked plaintext identity", err)
	}
	for _, role := range []string{"Master", "Follower"} {
		n.Role = role
		if err := n.CheckNativeRuntime(ctx, dir); err != nil {
			t.Fatal("native promoted role cannot restart", role, err)
		}
	}
}

func TestNativeRuntimeReceiptRejectsForeignVaultStoreIdentityAndTampering(t *testing.T) {
	dir, n := admissionFixture(t)
	other, foreign := admissionFixture(t)
	ctx := context.Background()
	if err := n.RecordNativeRuntime(ctx, dir); err != nil {
		t.Fatal(err)
	}
	n.Role = "Master"
	foreign.Role = "Master"
	if err := foreign.CheckNativeRuntime(ctx, dir); !errors.Is(err, ErrRuntimeAdmission) {
		t.Fatal("foreign vault accepted", err)
	}
	previous := n.ID
	n.ID = strings.Repeat("f", 32)
	if err := n.CheckNativeRuntime(ctx, dir); !errors.Is(err, ErrRuntimeAdmission) {
		t.Fatal("foreign cluster identity accepted", err)
	}
	n.ID = previous
	binding, err := os.ReadFile(filepath.Join(other, deployment.Binding))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, deployment.Binding), binding, 0600); err != nil {
		t.Fatal(err)
	}
	if err := n.CheckNativeRuntime(ctx, dir); !errors.Is(err, ErrRuntimeAdmission) {
		t.Fatal("foreign store accepted", err)
	}
	if err := os.WriteFile(filepath.Join(dir, runtimeReceipt), []byte("malformed"), 0600); err != nil {
		t.Fatal(err)
	}
	n.Role = "Standalone"
	if err := n.RecordNativeRuntime(ctx, dir); !errors.Is(err, ErrRuntimeAdmission) {
		t.Fatal("tampered receipt overwritten", err)
	}
}

func TestNativeRuntimeReceiptResetAndCancellation(t *testing.T) {
	dir, n := admissionFixture(t)
	ctx := context.Background()
	if err := n.RecordNativeRuntime(ctx, dir); err != nil {
		t.Fatal(err)
	}
	old, _ := os.ReadFile(filepath.Join(dir, runtimeReceipt))
	n.ID = strings.Repeat("e", 32) // Confirmed SQL reset has returned to Standalone.
	if err := n.CheckNativeRuntime(ctx, dir); err != nil {
		t.Fatal("standalone reset blocked", err)
	}
	if err := n.RecordNativeRuntime(ctx, dir); err != nil {
		t.Fatal(err)
	}
	n.Role = "Master"
	if err := n.CheckNativeRuntime(ctx, dir); err != nil {
		t.Fatal("new identity receipt not durable", err)
	}
	if err := os.WriteFile(filepath.Join(dir, runtimeReceipt), old, 0600); err != nil {
		t.Fatal(err)
	}
	if err := n.CheckNativeRuntime(ctx, dir); !errors.Is(err, ErrRuntimeAdmission) {
		t.Fatal("old reset identity replay admitted", err)
	}
	cancelled, cancel := context.WithCancel(ctx)
	cancel()
	if err := n.CheckNativeRuntime(cancelled, dir); err == nil {
		t.Fatal("cancelled admission succeeded")
	}
}

func TestNativeRuntimeReceiptRejectsNonRegularObjects(t *testing.T) {
	for _, name := range []string{runtimeReceipt, runtimeReceiptLock, deployment.Binding} {
		t.Run(name, func(t *testing.T) {
			dir, n := admissionFixture(t)
			path := filepath.Join(dir, name)
			if name == deployment.Binding {
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
			}
			if err := os.Mkdir(path, 0700); err != nil {
				t.Fatal(err)
			}
			if err := n.RecordNativeRuntime(context.Background(), dir); !errors.Is(err, ErrRuntimeAdmission) {
				t.Fatal("directory accepted", err)
			}
		})
	}
}

func TestNativeAdmissionDoesNotUpgradeTheSharedLifecycleLease(t *testing.T) {
	dir, n := admissionFixture(t)
	gate, err := maintenance.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	runtime, err := gate.Runtime(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer runtime.Close()
	if err := n.CheckNativeRuntime(runtime.Context(), dir); err != nil {
		t.Fatal("startup admission blocked on its own lifecycle lease", err)
	}
	if err := n.RecordNativeRuntime(runtime.Context(), dir); err != nil {
		t.Fatal(err)
	}
}

func TestNativeRuntimeReceiptConcurrentPublicationKeepsOneValidProof(t *testing.T) {
	dir, n := admissionFixture(t)
	var workers sync.WaitGroup
	results := make(chan error, 16)
	for i := 0; i < cap(results); i++ {
		workers.Add(1)
		go func() { defer workers.Done(); results <- n.RecordNativeRuntime(context.Background(), dir) }()
	}
	workers.Wait()
	close(results)
	for err := range results {
		if err != nil {
			t.Fatal(err)
		}
	}
	n.Role = "Follower"
	if err := n.CheckNativeRuntime(context.Background(), dir); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), runtimeReceipt+"-write-") {
			t.Fatal("private publication temporary survived")
		}
	}
}
