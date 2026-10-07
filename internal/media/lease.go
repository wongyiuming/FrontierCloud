package media

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
)

// Native RWMutex establishes Go's memory-order boundary; the OS lease protects
// filesystem and path metadata across independent runtime processes.
func (s *Service) acquire(ctx context.Context, write bool) (func(), error) {
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		acquired := false
		if write {
			acquired = s.mutation.TryLock()
		} else {
			acquired = s.mutation.TryRLock()
		}
		if acquired {
			break
		}
		timer := time.NewTimer(25 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
	localRelease := s.mutation.RUnlock
	if write {
		localRelease = s.mutation.Unlock
	}
	if info, err := s.root.Lstat(".media-mutation.lock"); err == nil && !info.Mode().IsRegular() {
		localRelease()
		return nil, ErrRecovery
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		localRelease()
		return nil, err
	}
	f, err := s.root.OpenFile(".media-mutation.lock", os.O_RDWR|os.O_CREATE, 0600)
	if err != nil {
		localRelease()
		return nil, err
	}
	opened, err := f.Stat()
	current, pathErr := s.root.Lstat(".media-mutation.lock")
	if err != nil || pathErr != nil || !current.Mode().IsRegular() || !os.SameFile(opened, current) {
		f.Close()
		localRelease()
		return nil, ErrRecovery
	}
	if err := fsutil.InheritOwner(f, s.root); err != nil {
		f.Close()
		localRelease()
		return nil, err
	}
	release, err := filelease.Acquire(ctx, f, write)
	if err != nil {
		localRelease()
		return nil, err
	}
	return func() { release(); localRelease() }, nil
}
func (s *Service) markRecovery() {
	s.recoveryRequired = true
	info, err := s.root.Lstat(".recovery-required")
	if err == nil && !info.Mode().IsRegular() {
		slog.Error("unsafe media recovery flag")
		return
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		slog.Error("media recovery flag unavailable", "error", err)
		return
	}
	f, err := s.root.OpenFile(".recovery-required", os.O_CREATE|os.O_WRONLY, 0600)
	if err == nil {
		err = f.Sync()
		f.Close()
	}
	if err == nil {
		err = s.syncDirectory(".")
	}
	if err != nil {
		slog.Error("media recovery flag persistence failed", "error", err)
	}
}
