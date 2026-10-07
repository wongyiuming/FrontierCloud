// Package filelease coordinates processes sharing a local data volume. Locks
// are released by the OS on crash; SQLite/media volumes must not be on NFS.
package filelease

import (
	"context"
	"errors"
	"os"
	"sync"
	"time"
)

var ErrBusy = errors.New("file lease is busy")

// Acquire owns file, including on failure. Call release exactly once (the
// returned function is also safe to call repeatedly).
func Acquire(ctx context.Context, file *os.File, exclusive bool) (func(), error) {
	for {
		if err := ctx.Err(); err != nil {
			file.Close()
			return nil, err
		}
		err := try(file, exclusive)
		if err == nil {
			var once sync.Once
			return func() { once.Do(func() { unlock(file); file.Close() }) }, nil
		}
		if !errors.Is(err, ErrBusy) {
			file.Close()
			return nil, err
		}
		timer := time.NewTimer(25 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			file.Close()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
}
func Try(file *os.File, exclusive bool) (func(), error) {
	if err := try(file, exclusive); err != nil {
		file.Close()
		return nil, err
	}
	var once sync.Once
	return func() { once.Do(func() { unlock(file); file.Close() }) }, nil
}
