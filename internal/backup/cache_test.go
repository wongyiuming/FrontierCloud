package backup

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCacheCleanupPreservesLiveArtifactsAndUnknownFiles(t *testing.T) {
	media := t.TempDir()
	os.Mkdir(filepath.Join(media, "lyrics"), 0700)
	r, err := os.OpenRoot(media)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	cache := t.TempDir()
	b, err := New(fixtureRepository{export: func(func(string, map[string]any) error) error { return nil }}, fixtureSource{r}, cache)
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close()
	a, err := b.Build(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	ctx, stop := context.WithTimeout(context.Background(), 75*time.Millisecond)
	defer stop()
	if _, err := CleanupCache(ctx, cache, false); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("live artifact erased", err)
	}
	if _, err := a.Read(make([]byte, 1)); err != nil {
		t.Fatal("live reader interrupted", err)
	}
	if err := a.Close(); err != nil {
		t.Fatal(err)
	}
	unknown := filepath.Join(cache, "business-"+strings.Repeat("a", 32)+".jsonl")
	os.WriteFile(unknown, []byte("old unclaimed artifact"), 0600)
	result, err := CleanupCache(context.Background(), cache, false)
	if err != nil || result.Removed != 0 || result.Retained != 1 {
		t.Fatal(result, err)
	}
	if data, _ := os.ReadFile(unknown); string(data) != "old unclaimed artifact" {
		t.Fatal("unknown artifact changed")
	}
}

func TestCacheCrashHelper(t *testing.T) {
	kind, directory := os.Getenv("FRONTIERCLOUD_CACHE_HELPER"), os.Getenv("FRONTIERCLOUD_CACHE_DIRECTORY")
	if kind == "" {
		t.Skip("native subprocess fixture")
	}
	if kind == "scratch" {
		_, err := Preflight(context.Background(), crashCacheReader{}, Expectation{1, strings.Repeat("a", 64), 100}, directory)
		t.Fatal("helper unexpectedly returned", err)
	}
	media := filepath.Join(directory, "../fixture-media")
	os.Mkdir(media, 0700)
	os.Mkdir(filepath.Join(media, "lyrics"), 0700)
	r, err := os.OpenRoot(media)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	b, err := New(fixtureRepository{export: func(func(string, map[string]any) error) error {
		fmt.Println("CACHE_READY")
		time.Sleep(time.Hour)
		return io.EOF
	}}, fixtureSource{r}, directory)
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close()
	_, err = b.Build(context.Background())
	t.Fatal("helper unexpectedly returned", err)
}

type crashCacheReader struct{}

func (crashCacheReader) Read([]byte) (int, error) {
	fmt.Println("CACHE_READY")
	time.Sleep(time.Hour)
	return 0, io.EOF
}

func TestKilledNativeCacheWritersReleaseTheirLeases(t *testing.T) {
	for _, kind := range []string{"artifact", "scratch"} {
		t.Run(kind, func(t *testing.T) {
			cache := filepath.Join(t.TempDir(), "cache")
			os.Mkdir(cache, 0700)
			ctx, stop := context.WithTimeout(context.Background(), 20*time.Second)
			defer stop()
			command := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestCacheCrashHelper$")
			command.Env = append(os.Environ(), "FRONTIERCLOUD_CACHE_HELPER="+kind, "FRONTIERCLOUD_CACHE_DIRECTORY="+cache)
			var diagnostic bytes.Buffer
			command.Stderr = &diagnostic
			output, err := command.StdoutPipe()
			if err != nil {
				t.Fatal(err)
			}
			if err = command.Start(); err != nil {
				t.Fatal(err)
			}
			defer command.Process.Kill()
			scanner := bufio.NewScanner(output)
			if !scanner.Scan() || scanner.Text() != "CACHE_READY" {
				command.Process.Kill()
				command.Wait()
				t.Fatal("native fixture did not become live", diagnostic.String())
			}
			live, cancel := context.WithTimeout(context.Background(), 75*time.Millisecond)
			_, err = CleanupCache(live, cache, kind == "scratch")
			cancel()
			if !errors.Is(err, context.DeadlineExceeded) {
				command.Process.Kill()
				command.Wait()
				t.Fatal("live cache removed", err)
			}
			if err = command.Process.Kill(); err != nil {
				t.Fatal(err)
			}
			command.Wait()
			result, err := CleanupCache(context.Background(), cache, kind == "scratch")
			if err != nil || result.Removed != 1 || result.Retained != 0 {
				t.Fatal("killed fixture did not clean", result, err)
			}
			checkScratchEmpty(t, cache)
		})
	}
}

func TestScratchCleanupRejectsUnknownChildrenMarkersAndSymlinks(t *testing.T) {
	for _, kind := range []string{"unclaimed", "foreign-marker", "unknown-child", "symlink-child"} {
		t.Run(kind, func(t *testing.T) {
			cache := t.TempDir()
			name := "preflight-" + strings.Repeat("a", 32)
			child := filepath.Join(cache, name)
			os.Mkdir(child, 0700)
			os.WriteFile(filepath.Join(child, "check.sqlite"), []byte("keep"), 0600)
			if kind != "unclaimed" {
				os.WriteFile(filepath.Join(child, ".owner"), []byte(scratchOwner), 0600)
			}
			switch kind {
			case "foreign-marker":
				os.WriteFile(filepath.Join(child, ".owner"), []byte("foreign"), 0600)
			case "unknown-child":
				os.WriteFile(filepath.Join(child, "user-data"), []byte("keep"), 0600)
			case "symlink-child":
				os.Remove(filepath.Join(child, "check.sqlite"))
				if err := os.Symlink(filepath.Join(cache, "outside"), filepath.Join(child, "check.sqlite")); err != nil {
					t.Skip("symlink privilege unavailable")
				}
			}
			result, err := CleanupCache(context.Background(), cache, true)
			if err != nil || result.Removed != 0 || result.Retained != 1 {
				t.Fatal(result, err)
			}
			if _, err := os.Lstat(child); err != nil {
				t.Fatal("unknown child erased", err)
			}
		})
	}
}
