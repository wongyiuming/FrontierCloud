// Package maintenance fences native processes sharing one local DATA_ROOT.
// It is not proof that legacy or remote database writers have stopped.
package maintenance

import (
	"context"
	"errors"
	"io"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
)

const Marker = ".frontiercloud-native-maintenance"
const markerContent = "frontiercloud-native-maintenance-v1\n"
const runtimeLock = ".native-runtime.lock"
const controlLock = ".native-maintenance-control.lock"

var ErrEnabled = errors.New("native runtime maintenance is enabled")
var ErrState = errors.New("unsafe native maintenance state")

type Gate struct{ root *os.Root }

func Open(directory string) (*Gate, error) {
	if directory == "" {
		return nil, ErrState
	}
	if err := os.MkdirAll(directory, 0750); err != nil {
		return nil, err
	}
	info, err := os.Lstat(directory)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, ErrState
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		return nil, err
	}
	return &Gate{root}, nil
}
func (g *Gate) Close() error { return g.root.Close() }

func (g *Gate) lease(ctx context.Context, name string, exclusive bool) (func(), error) {
	if info, err := g.root.Lstat(name); err == nil && !info.Mode().IsRegular() {
		return nil, ErrState
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	f, err := g.root.OpenFile(name, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	opened, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, err
	}
	current, err := g.root.Lstat(name)
	if err != nil || !current.Mode().IsRegular() || !os.SameFile(opened, current) {
		f.Close()
		return nil, ErrState
	}
	if err := fsutil.InheritOwner(f, g.root); err != nil {
		f.Close()
		return nil, err
	}
	return filelease.Acquire(ctx, f, exclusive)
}

// Any unexpected marker type/content fails closed, including directories and
// inside-root symlinks. UI force-open flags are not consulted by this gate.
func (g *Gate) Enabled() (bool, error) {
	info, err := g.root.Lstat(Marker)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return true, err
	}
	if !info.Mode().IsRegular() || info.Size() != int64(len(markerContent)) {
		return true, ErrState
	}
	f, err := g.root.Open(Marker)
	if err != nil {
		return true, err
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return true, ErrState
	}
	data, err := io.ReadAll(io.LimitReader(f, int64(len(markerContent))+1))
	if err != nil {
		return true, err
	}
	if string(data) != markerContent {
		return true, ErrState
	}
	return true, nil
}
func (g *Gate) enable() error {
	if enabled, err := g.Enabled(); err != nil {
		return err
	} else if enabled {
		f, err := g.root.OpenFile(Marker, os.O_RDWR, 0600)
		if err != nil {
			return err
		}
		return errors.Join(f.Sync(), f.Close(), fsutil.SyncDirectory(g.root, "."))
	}
	f, err := g.root.OpenFile(Marker, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	if err := fsutil.InheritOwner(f, g.root); err != nil {
		f.Close()
		return err
	}
	_, writeErr := f.WriteString(markerContent)
	if writeErr == nil {
		writeErr = f.Sync()
	}
	return errors.Join(writeErr, f.Close(), fsutil.SyncDirectory(g.root, "."))
}

// Enter persists the fence BEFORE waiting for runtime leases. Timeout/cancel or
// a failed callback leaves the fence enabled. Proof is valid only while the
// exclusive lease is held; no runtime can start between drain and callback.
func (g *Gate) Enter(ctx context.Context, offline func(context.Context) error) error {
	if offline == nil {
		return ErrState
	}
	release, err := g.lease(ctx, controlLock, true)
	if err != nil {
		return err
	}
	defer release()
	if err := g.enable(); err != nil {
		return err
	}
	return g.offline(ctx, offline)
}
func (g *Gate) Inspect(ctx context.Context, offline func(context.Context) error) error {
	release, err := g.lease(ctx, controlLock, true)
	if err != nil {
		return err
	}
	defer release()
	return g.offline(ctx, offline)
}
func (g *Gate) offline(ctx context.Context, offline func(context.Context) error) error {
	if offline == nil {
		return ErrState
	}
	if enabled, err := g.Enabled(); err != nil {
		return err
	} else if !enabled {
		return ErrState
	}
	release, err := g.lease(ctx, runtimeLock, true)
	if err != nil {
		return err
	}
	defer release()
	if err := ctx.Err(); err != nil {
		return err
	}
	return offline(ctx)
}

// Resume does not discard any intent or create recovery authority. Its check
// runs while stopped; rejection keeps the persistent fence closed.
func (g *Gate) Resume(ctx context.Context, check func(context.Context) error) error {
	release, err := g.lease(ctx, controlLock, true)
	if err != nil {
		return err
	}
	defer release()
	return g.offline(ctx, func(ctx context.Context) error {
		if check == nil {
			return ErrState
		}
		if err := check(ctx); err != nil {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := g.root.Remove(Marker); err != nil {
			return err
		}
		if err := fsutil.SyncDirectory(g.root, "."); err != nil {
			// A failed durability acknowledgement must not leave a knowingly open
			// gate. Best-effort re-fence and surface BOTH failures.
			return errors.Join(err, g.enable())
		}
		return nil
	})
}

type Runtime struct {
	gate     *Gate
	ctx      context.Context
	cancel   context.CancelCauseFunc
	release  func()
	done     chan struct{}
	mu       sync.Mutex
	stopping bool
	requests sync.WaitGroup
	once     sync.Once
}

// Runtime covers the ENTIRE native lifecycle, including initialization,
// startup recovery, HTTP, worker shutdown and resource closure. Caller must
// join workers and close resources before Close releases the process lease.
func (g *Gate) Runtime(parent context.Context) (*Runtime, error) {
	release, err := g.lease(parent, runtimeLock, false)
	if err != nil {
		return nil, err
	}
	if enabled, err := g.Enabled(); err != nil || enabled {
		release()
		if err != nil {
			return nil, err
		}
		return nil, ErrEnabled
	}
	ctx, cancel := context.WithCancelCause(parent)
	r := &Runtime{gate: g, ctx: ctx, cancel: cancel, release: release, done: make(chan struct{})}
	go func() {
		defer close(r.done)
		timer := time.NewTicker(100 * time.Millisecond)
		defer timer.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-timer.C:
				enabled, err := g.Enabled()
				if err != nil {
					cancel(err)
					return
				}
				if enabled {
					cancel(ErrEnabled)
					return
				}
			}
		}
	}()
	return r, nil
}
func (r *Runtime) Context() context.Context { return r.ctx }
func (r *Runtime) Stop()                    { r.mu.Lock(); r.stopping = true; r.mu.Unlock(); r.cancel(context.Canceled) }
func (r *Runtime) WaitHTTP()                { r.requests.Wait() }
func (r *Runtime) Close()                   { r.once.Do(func() { r.Stop(); <-r.done; r.WaitHTTP(); r.release() }) }

func (r *Runtime) Handler(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) {
		r.mu.Lock()
		enabled, err := r.gate.Enabled()
		blocked := r.stopping || r.ctx.Err() != nil || enabled || err != nil
		if !blocked {
			r.requests.Add(1)
		}
		r.mu.Unlock()
		if blocked {
			w.Header().Set("Cache-Control", "no-store")
			w.Header().Set("Retry-After", "5")
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusServiceUnavailable)
			io.WriteString(w, "{\"detail\":\"Native runtime is in maintenance\"}\n")
			return
		}
		defer r.requests.Done()
		ctx, cancel := context.WithCancel(q.Context())
		stop := context.AfterFunc(r.ctx, cancel)
		defer stop()
		defer cancel()
		next.ServeHTTP(w, q.WithContext(ctx))
	})
}
