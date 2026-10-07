package backup

import (
	"context"
	"errors"
	"io"
	"os"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

const cacheLock = ".cache.lock"
const artifactOwner = "native-backup-artifact-v1\n"
const scratchOwner = "native-backup-preflight-v1\n"

func removeOwnedCacheFile(root *os.Root, name string, owned os.FileInfo) error {
	current, err := root.Lstat(name)
	if err != nil {
		return err
	}
	if owned == nil || !current.Mode().IsRegular() || !os.SameFile(owned, current) {
		return store.ErrBackupState
	}
	return root.Remove(name)
}

func cacheLease(ctx context.Context, root *os.Root, exclusive bool) (func(), error) {
	return cacheNamedLease(ctx, root, cacheLock, exclusive)
}
func cacheNamedLease(ctx context.Context, root *os.Root, name string, exclusive bool) (func(), error) {
	info, err := root.Lstat(name)
	if err == nil && (!info.Mode().IsRegular() || info.Size() != 0) {
		return nil, store.ErrBackupState
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	f, err := root.OpenFile(name, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	opened, err := f.Stat()
	info, found := root.Lstat(name)
	if err != nil || found != nil || !info.Mode().IsRegular() || info.Size() != 0 || !os.SameFile(info, opened) {
		f.Close()
		return nil, store.ErrBackupState
	}
	if err := fsutil.InheritOwner(f, root); err != nil {
		f.Close()
		return nil, err
	}
	return filelease.Acquire(ctx, f, exclusive)
}

func exactCacheFile(root *os.Root, name string, expected string) (bool, error) {
	info, err := root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if !info.Mode().IsRegular() || info.Size() != int64(len(expected)) {
		return false, nil
	}
	f, err := root.Open(name)
	if err != nil {
		return false, err
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return false, store.ErrBackupState
	}
	raw, err := io.ReadAll(io.LimitReader(f, int64(len(expected))+1))
	return err == nil && string(raw) == expected, err
}

func writeCacheOwner(root *os.Root, name, content string) error {
	f, err := root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	if _, err = f.WriteString(content); err == nil {
		err = f.Sync()
	}
	return errors.Join(err, f.Close())
}

type CacheCleanup struct {
	Removed  int `json:"removed"`
	Retained int `json:"retained_unknown"`
}

// CleanupCache only recognizes newly claimed native artifact pairs or scratch
// children. Legacy/unclaimed files, live leases, symlinks and unknown children
// are never erased. It is cache maintenance, not cold backup retention/restore.
func CleanupCache(ctx context.Context, directory string, scratch bool) (CacheCleanup, error) {
	var result CacheCleanup
	info, err := os.Lstat(directory)
	if errors.Is(err, os.ErrNotExist) {
		return result, nil
	}
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return result, store.ErrBackupState
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		return result, err
	}
	defer root.Close()
	opened, err := root.Stat(".")
	if err != nil || !os.SameFile(info, opened) {
		return result, store.ErrBackupState
	}
	done, err := cacheLease(ctx, root, true)
	if err != nil {
		return result, err
	}
	defer done()
	dir, err := root.Open(".")
	if err != nil {
		return result, err
	}
	defer dir.Close()
	for {
		entries, readErr := dir.ReadDir(128)
		for _, entry := range entries {
			if err := ctx.Err(); err != nil {
				return result, err
			}
			name := entry.Name()
			if _, err := root.Lstat(name); errors.Is(err, os.ErrNotExist) {
				continue // Earlier pair removal can precede this directory entry.
			} else if err != nil {
				return result, err
			}
			if name == cacheLock || name == ".scheduler.lock" {
				continue // Retain OS lease inodes forever.
			}
			removed := false
			if scratch && strings.HasPrefix(name, "preflight-") && hexID(strings.TrimPrefix(name, "preflight-"), 32) {
				removed, err = cleanupScratch(root, name)
			} else if !scratch && strings.HasPrefix(name, "business-") && strings.HasSuffix(name, ".jsonl.owner") && hexID(strings.TrimSuffix(strings.TrimPrefix(name, "business-"), ".jsonl.owner"), 32) {
				removed, err = cleanupArtifact(root, strings.TrimSuffix(name, ".owner"))
			} else if !scratch && strings.HasPrefix(name, "business-") && strings.HasSuffix(name, ".jsonl") && hexID(strings.TrimSuffix(strings.TrimPrefix(name, "business-"), ".jsonl"), 32) {
				// The owner sidecar is the authoritative cleanup unit. Count only
				// unclaimed payloads here, avoiding double-counting its pair.
				owned, err := exactCacheFile(root, name+".owner", artifactOwner)
				if err != nil {
					return result, err
				}
				if owned {
					continue
				}
			}
			if err != nil {
				return result, err
			}
			if removed {
				result.Removed++
			} else {
				result.Retained++
			}
		}
		if readErr == io.EOF {
			return result, fsutil.SyncDirectory(root, ".")
		}
		if readErr != nil {
			return result, readErr
		}
	}
}

func cleanupArtifact(root *os.Root, name string) (bool, error) {
	owned, err := exactCacheFile(root, name+".owner", artifactOwner)
	if err != nil || !owned {
		return false, err
	}
	info, err := root.Lstat(name)
	if err == nil {
		if !info.Mode().IsRegular() {
			return false, nil
		}
		f, err := root.Open(name)
		if err != nil {
			return false, err
		}
		opened, e := f.Stat()
		f.Close()
		if e != nil || !os.SameFile(info, opened) {
			return false, store.ErrBackupState
		}
		if err := root.Remove(name); err != nil {
			return false, err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return false, err
	}
	// A crash after removing payload but before owner also converges safely.
	return true, root.Remove(name + ".owner")
}

func cleanupScratch(root *os.Root, name string) (bool, error) {
	info, err := root.Lstat(name)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return false, err
	}
	child, err := root.OpenRoot(name)
	if err != nil {
		return false, err
	}
	defer child.Close()
	opened, err := child.Stat(".")
	if err != nil || !os.SameFile(info, opened) {
		return false, store.ErrBackupState
	}
	owned, err := exactCacheFile(child, ".owner", scratchOwner)
	if err != nil || !owned {
		return false, err
	}
	f, err := child.Open(".")
	if err != nil {
		return false, err
	}
	entries, err := f.ReadDir(8)
	f.Close()
	if err != nil && err != io.EOF || len(entries) > 5 {
		return false, err
	}
	for _, e := range entries {
		if e.Name() != ".owner" && e.Name() != "check.sqlite" && e.Name() != "check.sqlite-wal" && e.Name() != "check.sqlite-shm" && e.Name() != "check.sqlite-journal" {
			return false, nil
		}
		i, err := child.Lstat(e.Name())
		if err != nil || !i.Mode().IsRegular() {
			return false, err
		}
	}
	current, err := root.Lstat(name)
	if err != nil || !os.SameFile(info, current) {
		return false, store.ErrBackupState
	}
	child.Close()
	// Exact validated generated private child, with only recognized native
	// staging files, and an exclusive parent cache lease. Never delete root.
	return true, root.RemoveAll(name)
}
