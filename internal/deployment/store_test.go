package deployment

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/filelease"
)

func fixture(t *testing.T) config.Config {
	t.Helper()
	r := t.TempDir()
	return config.Config{DataRoot: r, DatabaseType: config.DatabaseSQLite, SQLitePath: filepath.Join(r, "store.db"), SecretsDirectory: filepath.Join(r, "secrets")}
}

func TestSelectionIsDurableBeforeInitializationAndCannotSwitch(t *testing.T) {
	c := fixture(t)
	if err := Bind(context.Background(), c, nil); err != nil {
		t.Fatal(err)
	}
	// A failed connection/initialization has not opened a database or key.
	if _, err := os.Stat(c.SQLitePath); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("binding created a database", err)
	}
	if err := os.Mkdir(c.SecretsDirectory, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(c.SecretsDirectory, "node-vault.key"), []byte("crash after key publication"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := Bind(context.Background(), c, nil); err != nil {
		t.Fatal("same-store initialization cannot recover", err)
	}
	changed := c
	changed.SQLitePath = filepath.Join(c.DataRoot, "other.db")
	if err := Bind(context.Background(), changed, nil); !errors.Is(err, ErrSelection) {
		t.Fatal("different file admitted", err)
	}
	changed.DatabaseType, changed.MySQLHost, changed.MySQLPort, changed.MySQLDatabase = "mysql", "mysql", 3306, "fc"
	if err := Bind(context.Background(), changed, nil); !errors.Is(err, ErrSelection) {
		t.Fatal("backend switch admitted", err)
	}
}

func TestLegacyVaultRequiresExistingIdentityProof(t *testing.T) {
	c := fixture(t)
	os.Mkdir(c.SecretsDirectory, 0700)
	os.WriteFile(filepath.Join(c.SecretsDirectory, "node-vault.key"), []byte("existing key"), 0600)
	for _, proof := range []func(context.Context) error{nil, func(context.Context) error { return errors.New("absent or unrelated DB") }} {
		if err := Bind(context.Background(), c, proof); !errors.Is(err, ErrSelection) {
			t.Fatal("unproven identity admitted", err)
		}
		if _, err := os.Stat(filepath.Join(c.DataRoot, Binding)); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("refusal pinned wrong store", err)
		}
	}
	if err := Bind(context.Background(), c, func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
}

func TestConcurrentSelectorsChooseOneWinner(t *testing.T) {
	c := fixture(t)
	var wg sync.WaitGroup
	var mu sync.Mutex
	winner := ""
	for i := range 24 {
		candidate := c
		if i%2 == 0 {
			candidate.SQLitePath += ".alternate"
		}
		wg.Go(func() {
			err := Bind(context.Background(), candidate, nil)
			if err == nil {
				mu.Lock()
				defer mu.Unlock()
				if winner != "" && winner != candidate.SQLitePath {
					t.Error("two authoritative stores won")
				}
				winner = candidate.SQLitePath
			} else if !errors.Is(err, ErrSelection) {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if winner == "" {
		t.Fatal("no store admitted")
	}
}

func TestUnsafeBindingLockAndCancellationPreserveBytes(t *testing.T) {
	for _, name := range []string{Binding, lockName} {
		for _, content := range []string{"foreign", strings.Repeat("x", 1024)} {
			c := fixture(t)
			p := filepath.Join(c.DataRoot, name)
			os.WriteFile(p, []byte(content), 0600)
			if err := Bind(context.Background(), c, nil); !errors.Is(err, ErrSelection) {
				t.Fatal(name, err)
			}
			if got, _ := os.ReadFile(p); string(got) != content {
				t.Fatal("unknown file changed")
			}
		}
		c := fixture(t)
		target := filepath.Join(c.DataRoot, "unknown")
		os.WriteFile(target, []byte("preserve"), 0600)
		if err := os.Symlink(target, filepath.Join(c.DataRoot, name)); err == nil {
			if err := Bind(context.Background(), c, nil); !errors.Is(err, ErrSelection) {
				t.Fatal("symlink admitted", name, err)
			}
		}
	}
	c := fixture(t)
	f, err := os.OpenFile(filepath.Join(c.DataRoot, lockName), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	done, err := filelease.Acquire(context.Background(), f, true)
	if err != nil {
		t.Fatal(err)
	}
	defer done()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := Bind(ctx, c, nil); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(c.DataRoot, Binding)); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("cancelled selector published")
	}
}

func TestMySQLCredentialsDoNotChangeDatabaseSelection(t *testing.T) {
	c := fixture(t)
	c.DatabaseType, c.MySQLHost, c.MySQLPort, c.MySQLDatabase = "mysql", "MYSQL", 3306, "fc"
	if err := Bind(context.Background(), c, nil); err != nil {
		t.Fatal(err)
	}
	c.MySQLHost, c.MySQLUser, c.MySQLPasswordFile = "mysql", "rotated", "new-private-key"
	if err := Bind(context.Background(), c, nil); err != nil {
		t.Fatal("credential rotation blocked", err)
	}
	c.MySQLDatabase = "different"
	if err := Bind(context.Background(), c, nil); !errors.Is(err, ErrSelection) {
		t.Fatal("new database admitted", err)
	}
}
