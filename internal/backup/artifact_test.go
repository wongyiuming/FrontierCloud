package backup

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type fixtureRepository struct {
	store.BackupRepository
	export func(func(string, map[string]any) error) error
}

func (f fixtureRepository) ExportBusinessSnapshot(_ context.Context, emit func(string, map[string]any) error) error {
	return f.export(emit)
}

type fixtureSource struct{ root *os.Root }

func (f fixtureSource) WithBusinessBackup(ctx context.Context, emit func(*os.Root) error) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return emit(f.root)
}

func TestArtifactOversizedRecordCannotBecomeSendable(t *testing.T) {
	directory := t.TempDir()
	root, err := os.OpenRoot(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	cache := t.TempDir()
	repo := fixtureRepository{export: func(emit func(string, map[string]any) error) error {
		return emit("node_audit", map[string]any{"detail": strings.Repeat("x", MaxRecordBytes)})
	}}
	builder, err := New(repo, fixtureSource{root}, cache)
	if err != nil {
		t.Fatal(err)
	}
	defer builder.Close()
	if artifact, err := builder.Build(context.Background()); !errors.Is(err, store.ErrBackupState) || artifact != nil {
		t.Fatal("oversize became sendable", err)
	}
	checkScratchEmpty(t, cache)
}

func TestArtifactPrivateStreamingChecksumFooterLyricAndFailureCleanup(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "lyrics"), 0700); err != nil {
		t.Fatal(err)
	}
	lyric := "[00:01]备份歌词\x00\n"
	if err := os.WriteFile(filepath.Join(dir, "lyrics", "现场.lrc"), []byte(lyric), 0600); err != nil {
		t.Fatal(err)
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	repo := fixtureRepository{export: func(emit func(string, map[string]any) error) error {
		return emit("media_objects", map[string]any{"media_path": "music/现场/原曲.mp3", "media_id": strings.Repeat("a", 64)})
	}}
	cache := filepath.Join(t.TempDir(), "cache")
	builder, err := New(repo, fixtureSource{root}, cache)
	if err != nil {
		t.Fatal(err)
	}
	defer builder.Close()
	artifact, err := builder.Build(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if info, err := os.Stat(filepath.Join(cache, artifact.name)); err != nil || runtime.GOOS != "windows" && info.Mode().Perm()&0077 != 0 {
		t.Fatal("artifact permissions", info, err)
	}
	data, err := io.ReadAll(artifact)
	if err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(data)
	if artifact.Checksum != hex.EncodeToString(hash[:]) || artifact.Bytes != int64(len(data)) {
		t.Fatal("artifact digest", artifact)
	}
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	decoder.UseNumber()
	var header, row, file, end map[string]any
	for _, target := range []*map[string]any{&header, &row, &file, &end} {
		if err := decoder.Decode(target); err != nil {
			t.Fatal(err)
		}
	}
	if header["version"] != json.Number("2") || row["table"] != "media_objects" || file["name"] != "现场.lrc" || file["payload"] != base64.StdEncoding.EncodeToString([]byte(lyric)) || end["kind"] != "end" {
		t.Fatal("v2 artifact contract", header, row, file, end)
	}
	if err := artifact.Close(); err != nil {
		t.Fatal(err)
	}
	if err := artifact.Close(); err != nil {
		t.Fatal("close replay", err)
	}
	if _, err := os.Stat(filepath.Join(cache, artifact.name)); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("private artifact retained", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "lyrics", "too-large.lrc"), []byte(strings.Repeat("x", int(MaxLyricBytes+1))), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := builder.Build(context.Background()); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("unbounded lyric", err)
	}
	checkScratchEmpty(t, cache)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := builder.Build(ctx); !errors.Is(err, context.Canceled) {
		t.Fatal("cancel ignored", err)
	}
	release, err := builder.SchedulerLease(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	second, err := New(repo, fixtureSource{root}, cache)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	deadline, stop := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer stop()
	if release, err := second.SchedulerLease(deadline); err == nil {
		release()
		t.Fatal("two backup workers own one cache")
	}
}
