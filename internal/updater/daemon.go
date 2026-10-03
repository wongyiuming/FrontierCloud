package updater

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/release"
)

type Request struct {
	Action   string            `json:"action"`
	Target   string            `json:"target_sha,omitempty"`
	Mode     string            `json:"mode,omitempty"`
	Hold     bool              `json:"hold_maintenance,omitempty"`
	Manifest *release.Manifest `json:"release_manifest,omitempty"`
}
type Checkpoint struct {
	State, Phase, Current, Previous string
	SetPrevious                     bool
}
type Outcome struct {
	Current, Previous string
	Handoff           bool
}
type Executor interface {
	Execute(context.Context, Request, Status, func(Checkpoint) error) (Outcome, error)
	Recover(context.Context, Status) error
}
type Daemon struct {
	store       *privateStore
	maintenance *privateStore
	executor    Executor
	status      Status
	mu          sync.Mutex
	queue       chan Request
	directory   string
	fault       bool
}

func NewDaemon(ctx context.Context, directory, maintenanceDirectory, runtime, branch string, executor Executor) (*Daemon, error) {
	if !release.ValidSHA(runtime) || (branch != "main" && branch != "gin_main") || executor == nil {
		return nil, ErrState
	}
	s, err := openPrivate(directory, true)
	if err != nil {
		return nil, err
	}
	m, err := openPrivate(maintenanceDirectory, false)
	if err != nil {
		s.Close()
		return nil, err
	}
	d := &Daemon{store: s, maintenance: m, executor: executor, queue: make(chan Request, 1), directory: directory}
	if err = directoryPermissions(directory); err != nil {
		d.Close()
		return nil, err
	}
	// Nginx's unprivileged worker only stats this flag. Directory traversal is
	// required; the flag contains a public release SHA, never private snapshots.
	if err = os.Chmod(maintenanceDirectory, 0755); err != nil {
		d.Close()
		return nil, err
	}
	recovered := false
	if err = s.read("status.json", 8192, &d.status); errors.Is(err, os.ErrNotExist) {
		d.status = Status{State: "idle", Phase: "idle", CurrentSHA: runtime, RuntimeSHA: runtime, ReleaseBranch: branch}
	} else if err != nil || !d.status.valid() || d.status.ReleaseBranch != branch {
		m.writeBytes("enabled", []byte("unsafe-updater-state\n"))
		d.Close()
		return nil, ErrState
	} else if release.Busy(d.status.State) {
		recovered = true
		// No in-memory task survives a process crash. Never silently requeue or
		// report success just because services were previously replaced.
		if err = m.writeBytes("enabled", []byte(d.status.TargetSHA+"\n")); err != nil {
			d.Close()
			return nil, err
		}
		if d.status.State == "restarting" && runtime == d.status.TargetSHA {
			if err = executor.Recover(ctx, d.status); err == nil {
				d.status.State, d.status.Phase, d.status.Detail = "success", "complete", ""
				d.status.CompletedAt = time.Now().Unix()
				// Persist success before opening traffic. A crash before removal
				// leaves a closed flag; the success path below rechecks recovery.
			} else {
				d.status.State, d.status.Phase, d.status.Detail = "failed", "interrupted", "updater recovery requires attention"
			}
		} else {
			err = executor.Recover(ctx, d.status)
			d.status.State, d.status.Phase, d.status.Detail = "failed", "interrupted", "release interrupted; maintenance retained"
			if err != nil {
				d.status.Detail = "release interrupted; recovery requires attention"
			}
		}
	}
	if d.status.State == "failed" {
		if err = m.writeBytes("enabled", []byte("release-failed\n")); err != nil {
			d.Close()
			return nil, err
		}
		if !recovered {
			if err = executor.Recover(ctx, d.status); err != nil {
				d.status.Detail = "failed release recovery requires attention"
			}
		}
	}
	d.status.RuntimeSHA = runtime
	if err = d.persistLocked(); err != nil {
		d.Close()
		return nil, err
	}
	if d.status.State == "success" {
		// Recovery also proves the runtime/service generation before reopening
		// a success whose process died before durable flag removal.
		if runtime != d.status.CurrentSHA || executor.Recover(ctx, d.status) != nil {
			d.maintenance.writeBytes("enabled", []byte("recovery-required\n"))
			d.Close()
			return nil, ErrState
		}
		if err = m.remove("enabled"); err != nil {
			d.Close()
			return nil, err
		}
	}
	return d, nil
}
func (d *Daemon) Close() error { return errors.Join(d.maintenance.Close(), d.store.Close()) }
func (d *Daemon) persistLocked() error {
	d.status.UpdatedAt = time.Now().Unix()
	err := d.store.write("status.json", d.status, 8192)
	if err != nil {
		d.fault = true
	}
	return err
}
func (d *Daemon) Status() Status {
	d.mu.Lock()
	defer d.mu.Unlock()
	copy := d.status
	copy.TargetManifest = release.CloneManifest(copy.TargetManifest)
	copy.CurrentManifest = release.CloneManifest(copy.CurrentManifest)
	copy.PreviousManifest = release.CloneManifest(copy.PreviousManifest)
	return copy
}
func (d *Daemon) Start(request Request) (map[string]any, error) {
	if request.Mode != "upgrade" && request.Mode != "rollback" {
		return nil, ErrState
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if request.Manifest != nil {
		policy, err := release.PolicyForBranch(d.status.ReleaseBranch)
		if err != nil {
			return nil, ErrState
		}
		cloned := release.CloneManifest(request.Manifest)
		if cloned == nil {
			return nil, ErrState
		}
		artifact, err := cloned.Select(policy)
		if err != nil || request.Target != "" && request.Target != artifact.CommitSHA {
			return nil, ErrState
		}
		request.Manifest, request.Target = cloned, artifact.CommitSHA
	}
	if !release.ValidSHA(request.Target) {
		return nil, ErrState
	}
	if d.fault {
		return nil, ErrState
	}
	if release.Busy(d.status.State) {
		return map[string]any{"ok": false, "reason": "release already running", "status": d.status}, nil
	}
	old := d.status
	d.status.State, d.status.Phase, d.status.TargetSHA, d.status.Mode, d.status.HoldMaintenance, d.status.Detail = "queued", "queued", request.Target, request.Mode, request.Hold, ""
	d.status.TargetManifest = request.Manifest
	d.status.CompletedAt = 0
	if err := d.persistLocked(); err != nil {
		d.status = old
		return nil, err
	}
	// Publish a durable intent before waking the worker, under the same mutex.
	select {
	case d.queue <- request:
		out := map[string]any{"ok": true, "accepted": true, "target_sha": request.Target, "mode": request.Mode}
		if request.Manifest != nil {
			out["release_id"], _ = request.Manifest.ID()
		}
		return out, nil
	default:
		return nil, ErrState
	}
}
func (d *Daemon) checkpoint(c Checkpoint) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	previous := d.status
	if c.State != "" {
		d.status.State = c.State
	}
	if c.Phase != "" {
		d.status.Phase = c.Phase
	}
	if c.Current != "" {
		d.publishManifest(c.Current)
		d.status.CurrentSHA = c.Current
	}
	if c.Previous != "" || c.SetPrevious {
		d.status.PreviousSHA = c.Previous
	}
	if !d.status.valid() {
		d.status = previous
		return ErrState
	}
	if err := d.persistLocked(); err != nil {
		d.status = previous
		return err
	}
	return nil
}

// Manifest release history is independent of artifact SHA history: a common
// release may change another profile while this node's artifact stays the same.
func (d *Daemon) publishManifest(current string) {
	if d.status.TargetManifest != nil && current == d.status.TargetSHA {
		newID, _ := d.status.TargetManifest.ID()
		oldID := ""
		if d.status.CurrentManifest != nil {
			oldID, _ = d.status.CurrentManifest.ID()
		}
		if newID != oldID {
			if d.status.Mode == "upgrade" {
				d.status.PreviousManifest = release.CloneManifest(d.status.CurrentManifest)
			} else {
				d.status.PreviousManifest = nil
			}
			d.status.CurrentManifest = release.CloneManifest(d.status.TargetManifest)
		}
	} else if current != d.status.CurrentSHA {
		d.status.PreviousManifest = release.CloneManifest(d.status.CurrentManifest)
		d.status.CurrentManifest = nil
	}
}
func (d *Daemon) perform(ctx context.Context, r Request) {
	d.mu.Lock()
	d.status.State, d.status.Phase, d.status.StartedAt = "running", "validating", time.Now().Unix()
	before := d.status
	err := d.persistLocked()
	d.mu.Unlock()
	if err == nil {
		err = d.maintenance.writeBytes("enabled", []byte(r.Target+"\n"))
	}
	var outcome Outcome
	if err == nil {
		outcome, err = d.executor.Execute(ctx, r, before, d.checkpoint)
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if err != nil {
		// A handoff helper may terminate this old runtime after its durable
		// restart checkpoint. Cancellation must not overwrite that checkpoint.
		if d.status.State == "restarting" && ctx.Err() != nil {
			return
		}
		d.status.State, d.status.Phase, d.status.Detail = "failed", "failed", "release failed; maintenance retained"
		d.persistLocked()
		return
	}
	if outcome.Current != r.Target || !release.ValidSHA(outcome.Current) || (outcome.Previous != "" && !release.ValidSHA(outcome.Previous)) {
		d.status.State, d.status.Phase, d.status.Detail = "failed", "failed", "release proof incomplete"
		d.persistLocked()
		return
	}
	d.publishManifest(outcome.Current)
	d.status.CurrentSHA, d.status.PreviousSHA = outcome.Current, outcome.Previous
	if outcome.Handoff {
		d.status.State, d.status.Phase = "restarting", "updater-restart"
		d.persistLocked()
		return
	}
	if d.status.RuntimeSHA != r.Target {
		d.status.State, d.status.Phase, d.status.Detail = "failed", "failed", "updater runtime handoff missing"
		d.persistLocked()
		return
	}
	d.status.State, d.status.Phase, d.status.Detail, d.status.CompletedAt = "success", "complete", "", time.Now().Unix()
	if err = d.persistLocked(); err == nil {
		err = d.maintenance.remove("enabled")
	}
	if err != nil {
		d.maintenance.writeBytes("enabled", []byte(r.Target+"\n"))
		d.status.State, d.status.Phase, d.status.Detail = "failed", "failed", "release completion not durably acknowledged"
		d.persistLocked()
	}
}

func (d *Daemon) Serve(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	name := filepath.Join(d.directory, "control.sock")
	if info, err := os.Lstat(name); err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return ErrState
		}
		if err = os.Remove(name); err != nil {
			return err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	listener, err := net.Listen("unix", name)
	if err != nil {
		return err
	}
	defer listener.Close()
	if err = socketPermissions(name); err != nil {
		return err
	}
	stop := context.AfterFunc(ctx, func() { listener.Close() })
	defer stop()
	var worker, handlers sync.WaitGroup
	worker.Go(func() {
		for {
			select {
			case <-ctx.Done():
				return
			case r := <-d.queue:
				d.perform(ctx, r)
			}
		}
	})
	defer func() { cancel(); handlers.Wait(); worker.Wait() }()
	connections := make(chan struct{}, 16)
	for {
		conn, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		select {
		case connections <- struct{}{}:
		case <-ctx.Done():
			conn.Close()
			return nil
		default:
			conn.Close()
			continue
		}
		handlers.Go(func() { defer func() { conn.Close(); <-connections }(); d.handle(ctx, conn) })
	}
}
func (d *Daemon) handle(ctx context.Context, conn net.Conn) {
	conn.SetDeadline(time.Now().Add(3 * time.Second))
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	raw, err := bufio.NewReaderSize(conn, 8192).ReadSlice('\n')
	result := map[string]any{"ok": false, "reason": "invalid updater request"}
	if err == nil && len(raw) <= 8192 {
		value, e := protocol.ParseStrictJSON(raw, 8192)
		if e == nil {
			if object, ok := value.(map[string]any); ok {
				action, _ := object["action"].(string)
				switch action {
				case "status":
					if len(object) == 1 {
						result = map[string]any{"ok": true, "status": d.Status(), "capabilities": []string{release.ManifestCapability}}
					}
				case "start":
					target, tok := object["target_sha"].(string)
					mode, mok := object["mode"].(string)
					hold, hok := object["hold_maintenance"].(bool)
					if _, exists := object["hold_maintenance"]; !exists {
						hok = true
					}
					if tok && mok && hok && len(object) >= 3 && len(object) <= 4 {
						valid := true
						for key := range object {
							if key != "action" && key != "target_sha" && key != "mode" && key != "hold_maintenance" {
								valid = false
							}
						}
						if valid {
							if out, e := d.Start(Request{Action: "start", Target: target, Mode: mode, Hold: hold}); e == nil {
								result = out
							}
						}
					}
				}
			}
		}
	}
	json.NewEncoder(conn).Encode(result)
}
