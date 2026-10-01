// Package backup builds private, bounded-memory cold recovery artifacts.
package backup

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

const MaxLyricBytes int64 = 2 * 1024 * 1024

type Source interface {
	WithBusinessBackup(context.Context, func(*os.Root) error) error
}
type Builder struct {
	repository store.BackupRepository
	source     Source
	root       *os.Root
}
type Artifact struct {
	Generation int64
	Checksum   string
	Bytes      int64
	file       *os.File
	root       *os.Root
	name       string
	once       sync.Once
	err        error
}

func (a *Artifact) Read(p []byte) (int, error) { return a.file.Read(p) }
func (a *Artifact) Close() error {
	a.once.Do(func() { a.err = errors.Join(a.file.Close(), a.root.Remove(a.name)) })
	return a.err
}
func New(repository store.BackupRepository, source Source, directory string) (*Builder, error) {
	if repository == nil || source == nil || directory == "" {
		return nil, store.ErrBackupState
	}
	directory, err := filepath.Abs(directory)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	info, err := os.Lstat(directory)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, store.ErrBackupState
	}
	if err := os.Chmod(directory, 0700); err != nil {
		return nil, err
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		return nil, err
	}
	return &Builder{repository, source, root}, nil
}
func (b *Builder) Close() error { return b.root.Close() }
func (b *Builder) SchedulerLease(ctx context.Context) (func(), error) {
	if info, err := b.root.Lstat(".scheduler.lock"); err == nil && !info.Mode().IsRegular() {
		return nil, store.ErrBackupState
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	f, err := b.root.OpenFile(".scheduler.lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	return filelease.Acquire(ctx, f, true)
}
func (b *Builder) Build(ctx context.Context) (_ *Artifact, resultErr error) {
	var identifier [16]byte
	if _, err := rand.Read(identifier[:]); err != nil {
		return nil, err
	}
	name := "business-" + hex.EncodeToString(identifier[:]) + ".jsonl"
	f, err := b.root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	artifact := &Artifact{Generation: time.Now().UnixNano(), file: f, root: b.root, name: name}
	defer func() {
		if resultErr != nil {
			resultErr = errors.Join(resultErr, artifact.Close())
		}
	}()
	digest := sha256.New()
	output := bufio.NewWriterSize(io.MultiWriter(f, digest), 64*1024)
	encoder := json.NewEncoder(output)
	encoder.SetEscapeHTML(false)
	write := func(value any) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		return encoder.Encode(value)
	}
	err = b.source.WithBusinessBackup(ctx, func(root *os.Root) error {
		if err := write(map[string]any{"kind": "header", "version": 2, "generation": artifact.Generation}); err != nil {
			return err
		}
		if err := b.repository.ExportBusinessSnapshot(ctx, func(table string, row map[string]any) error {
			return write(map[string]any{"kind": "row", "table": table, "value": row})
		}); err != nil {
			return err
		}
		if err := walkLyrics(ctx, root, "lyrics", 0, func(name string) error {
			payload, err := readLyric(ctx, root, name)
			if err != nil {
				return err
			}
			return write(map[string]any{"kind": "lyric", "name": strings.TrimPrefix(name, "lyrics/"), "payload": base64.StdEncoding.EncodeToString(payload)})
		}); err != nil {
			return err
		}
		return write(map[string]any{"kind": "end"})
	})
	if err != nil {
		return nil, err
	}
	if err := output.Flush(); err != nil {
		return nil, err
	}
	if err := f.Sync(); err != nil {
		return nil, err
	}
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	artifact.Bytes, artifact.Checksum = info.Size(), hex.EncodeToString(digest.Sum(nil))
	return artifact, nil
}

func walkLyrics(ctx context.Context, root *os.Root, directory string, depth int, emit func(string) error) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if depth > 64 {
		return store.ErrBackupState
	}
	info, err := root.Lstat(directory)
	if err != nil {
		return err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return store.ErrBackupState
	}
	f, err := root.Open(directory)
	if err != nil {
		return err
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil {
		return err
	}
	if !os.SameFile(info, opened) {
		return store.ErrBackupState
	}
	for {
		entries, readErr := f.ReadDir(128)
		for _, entry := range entries {
			if err := ctx.Err(); err != nil {
				return err
			}
			if strings.HasPrefix(entry.Name(), ".") {
				continue
			}
			if !utf8.ValidString(entry.Name()) || entry.Type()&os.ModeSymlink != 0 {
				return store.ErrBackupState
			}
			name := directory + "/" + entry.Name()
			if entry.IsDir() {
				if err := walkLyrics(ctx, root, name, depth+1, emit); err != nil {
					return err
				}
			} else if path.Ext(name) == ".lrc" {
				if err := emit(name); err != nil {
					return err
				}
			}
		}
		if readErr == io.EOF {
			return nil
		}
		if readErr != nil {
			return readErr
		}
	}
}
func readLyric(ctx context.Context, root *os.Root, name string) ([]byte, error) {
	before, err := root.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !before.Mode().IsRegular() || before.Size() > MaxLyricBytes {
		return nil, store.ErrBackupState
	}
	f, err := root.Open(name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !os.SameFile(before, opened) {
		return nil, store.ErrBackupState
	}
	data, err := io.ReadAll(io.LimitReader(&contextReader{ctx, f}, MaxLyricBytes+1))
	if err != nil {
		return nil, err
	}
	after, err := root.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !after.Mode().IsRegular() || !os.SameFile(before, after) || after.Size() != before.Size() || !after.ModTime().Equal(before.ModTime()) || int64(len(data)) != before.Size() {
		return nil, store.ErrBackupState
	}
	return data, nil
}

type contextReader struct {
	ctx    context.Context
	source io.Reader
}

func (r *contextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.source.Read(p)
}
