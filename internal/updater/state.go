// Package updater owns the isolated Docker mutation agent. Business HTTP
// handlers have no Docker access. Its persistent queue is not business state.
package updater

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"sync"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/release"
)

var ErrState = errors.New("unsafe updater persistent state")

type Status struct {
	State           string `json:"state"`
	Phase           string `json:"phase"`
	Mode            string `json:"mode,omitempty"`
	TargetSHA       string `json:"target_sha,omitempty"`
	CurrentSHA      string `json:"current_sha"`
	PreviousSHA     string `json:"previous_sha"`
	RuntimeSHA      string `json:"updater_runtime_sha"`
	ReleaseBranch   string `json:"release_branch"`
	HoldMaintenance bool   `json:"hold_maintenance"`
	Detail          string `json:"detail"`
	UpdatedAt       int64  `json:"updated_at"`
	StartedAt       int64  `json:"started_at,omitempty"`
	CompletedAt     int64  `json:"completed_at,omitempty"`
}

func (s Status) valid() bool {
	switch s.State {
	case "idle", "queued", "running", "distributing", "restarting", "success", "failed":
	default:
		return false
	}
	if s.Phase == "" || len(s.Phase) > 64 || len(s.Detail) > 512 {
		return false
	}
	for _, sha := range []string{s.CurrentSHA, s.PreviousSHA, s.TargetSHA, s.RuntimeSHA} {
		if sha != "" && !release.ValidSHA(sha) {
			return false
		}
	}
	if !release.ValidSHA(s.RuntimeSHA) || !release.ValidSHA(s.CurrentSHA) {
		return false
	}
	if release.Busy(s.State) && (!release.ValidSHA(s.TargetSHA) || (s.Mode != "upgrade" && s.Mode != "rollback")) {
		return false
	}
	return s.ReleaseBranch == "main" || s.ReleaseBranch == "gin_main"
}

type privateStore struct {
	root    *os.Root
	mu      sync.Mutex
	release func()
}

func openPrivate(directory string, own bool) (*privateStore, error) {
	if directory == "" {
		return nil, ErrState
	}
	if err := os.MkdirAll(directory, 0750); err != nil {
		return nil, err
	}
	info, err := os.Lstat(directory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, ErrState
	}
	r, err := os.OpenRoot(directory)
	if err != nil {
		return nil, err
	}
	current, err := r.Stat(".")
	if err != nil || !os.SameFile(info, current) {
		r.Close()
		return nil, ErrState
	}
	s := &privateStore{root: r}
	if own {
		if info, err := r.Lstat(".runtime.lock"); err == nil && (!info.Mode().IsRegular() || info.Size() != 0) {
			r.Close()
			return nil, ErrState
		} else if err != nil && !errors.Is(err, os.ErrNotExist) {
			r.Close()
			return nil, err
		}
		f, err := r.OpenFile(".runtime.lock", os.O_CREATE|os.O_RDWR, 0600)
		if err != nil {
			r.Close()
			return nil, err
		}
		opened, e := f.Stat()
		found, e2 := r.Lstat(".runtime.lock")
		if e != nil || e2 != nil || !found.Mode().IsRegular() || !os.SameFile(opened, found) {
			f.Close()
			r.Close()
			return nil, ErrState
		}
		s.release, err = filelease.Try(f, true)
		if err != nil {
			r.Close()
			return nil, err
		}
	}
	return s, nil
}
func (s *privateStore) Close() error {
	if s.release != nil {
		s.release()
	}
	return s.root.Close()
}
func safeName(name string) bool {
	return name == "status.json" || name == "replacement.json" || name == "handoff.json" || name == "enabled"
}
func (s *privateStore) read(name string, maximum int64, value any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !safeName(name) {
		return ErrState
	}
	info, err := s.root.Lstat(name)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > maximum {
		return ErrState
	}
	f, err := s.root.Open(name)
	if err != nil {
		return err
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return ErrState
	}
	raw, err := io.ReadAll(io.LimitReader(f, maximum+1))
	if err != nil {
		return err
	}
	if _, err = protocol.ParseStrictJSON(raw, int(maximum)); err != nil {
		return ErrState
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	d.UseNumber()
	if d.Decode(value) != nil {
		return ErrState
	}
	return nil
}
func (s *privateStore) write(name string, value any, maximum int) error {
	raw, err := json.Marshal(value)
	if err != nil || len(raw) > maximum {
		return ErrState
	}
	return s.writeBytes(name, raw)
}
func (s *privateStore) writeBytes(name string, raw []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !safeName(name) {
		return ErrState
	}
	if info, err := s.root.Lstat(name); err == nil && !info.Mode().IsRegular() {
		return ErrState
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return err
	}
	temp := ".updater-write-" + hex.EncodeToString(nonce[:])
	f, err := s.root.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer s.root.Remove(temp)
	_, err = f.Write(raw)
	if err == nil {
		err = f.Sync()
	}
	err = errors.Join(err, f.Close())
	if err != nil {
		return err
	}
	if err = s.root.Rename(temp, name); err != nil {
		return err
	}
	return fsutil.SyncDirectory(s.root, ".")
}
func (s *privateStore) remove(name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !safeName(name) {
		return ErrState
	}
	info, err := s.root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return ErrState
	}
	if err = s.root.Remove(name); err != nil {
		return err
	}
	return fsutil.SyncDirectory(s.root, ".")
}
