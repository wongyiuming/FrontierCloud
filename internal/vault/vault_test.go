package vault

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func TestPythonFernetEnvelope(t *testing.T) {
	bytes, err := os.ReadFile("../../protocol/v2/vectors/fernet.json")
	if err != nil {
		t.Fatal(err)
	}
	var vector struct{ Key, Plaintext, Token string }
	if err := json.Unmarshal(bytes, &vector); err != nil {
		t.Fatal(err)
	}
	v, err := New(vector.Key)
	if err != nil {
		t.Fatal(err)
	}
	plain, err := v.Unseal(vector.Token)
	if err != nil || plain != vector.Plaintext {
		t.Fatalf("decrypt Python: %q %v", plain, err)
	}
	for _, value := range []string{"", vector.Plaintext, strings.Repeat("音乐", 2000)} {
		token, err := v.Seal(value)
		if err != nil {
			t.Fatal(err)
		}
		got, err := v.Unseal(token)
		if err != nil || got != value {
			t.Fatalf("roundtrip: %v", err)
		}
		raw := []byte(token)
		raw[35] ^= 1
		if _, err := v.Unseal(string(raw)); err == nil {
			t.Fatal("tampered envelope accepted")
		}
	}
	other, _ := New("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
	if _, err := other.Unseal(vector.Token); err == nil {
		t.Fatal("wrong key accepted")
	}
}

func TestConcurrentVaultPublishAndRestart(t *testing.T) {
	dir := t.TempDir()
	var wg sync.WaitGroup
	keys := make(chan [32]byte, 12)
	failures := make(chan error, 12)
	for range 12 {
		wg.Go(func() {
			v, err := Open(dir)
			if err != nil {
				failures <- err
				return
			}
			keys <- v.key
		})
	}
	wg.Wait()
	close(keys)
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	var expected [32]byte
	first := true
	for key := range keys {
		if first {
			expected = key
			first = false
		} else if expected != key {
			t.Fatal("concurrent startup generated different keys")
		}
	}
	v, err := Open(dir)
	if err != nil || v.key != expected {
		t.Fatalf("restart: %v", err)
	}
	info, err := os.Stat(filepath.Join(dir, "node-vault.key"))
	if err != nil || info.Size() != 44 {
		t.Fatalf("key file: %v", err)
	}
}
