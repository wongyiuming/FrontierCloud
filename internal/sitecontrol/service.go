// Package sitecontrol owns the public availability flags, not the stronger
// native offline recovery fence. Admin reopening cannot remove that fence.
package sitecontrol

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"os"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
	"github.com/wongyiuming/FrontierCloud/internal/release"
)

const Manual = ".frontiercloud-maintenance"
const ForceOpen = ".frontiercloud-force-open"
const Lock = ".site-control.lock"

var ErrState = errors.New("unsafe site maintenance state")

type Service struct {
	root  *os.Root
	agent release.Agent
}

func Open(directory string, agent release.Agent) (*Service, error) {
	info, err := os.Lstat(directory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, ErrState
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		return nil, err
	}
	found, err := root.Stat(".")
	if err != nil || !os.SameFile(info, found) {
		root.Close()
		return nil, ErrState
	}
	return &Service{root, agent}, nil
}
func (s *Service) Close() error { return s.root.Close() }

// ClearOverride is used by the isolated native updater before mutation. It
// shares the Admin operator lease and never erases an unrecognized flag.
func ClearOverride(ctx context.Context, root *os.Root) error {
	done, err := Lease(ctx, root, true)
	if err != nil {
		return err
	}
	defer done()
	s := Service{root: root}
	exists, err := s.flag(ForceOpen, "release-failure-override\n")
	if err != nil {
		return err
	}
	if !exists {
		return nil
	}
	return s.remove(ForceOpen)
}

// Lease also coordinates native updater override clearing. Its inode is never
// unlinked. Holding it across updater status prevents two Admin workers racing.
func Lease(ctx context.Context, root *os.Root, exclusive bool) (func(), error) {
	info, err := root.Lstat(Lock)
	if err == nil && (!info.Mode().IsRegular() || info.Size() != 0) {
		return nil, ErrState
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	f, err := root.OpenFile(Lock, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	opened, err := f.Stat()
	found, e := root.Lstat(Lock)
	if err != nil || e != nil || !found.Mode().IsRegular() || !os.SameFile(opened, found) {
		f.Close()
		return nil, ErrState
	}
	if err = fsutil.InheritOwner(f, root); err != nil {
		f.Close()
		return nil, err
	}
	return filelease.Acquire(ctx, f, exclusive)
}
func (s *Service) flag(name, content string) (bool, error) {
	info, err := s.root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if !info.Mode().IsRegular() || info.Size() != int64(len(content)) {
		return false, ErrState
	}
	f, err := s.root.Open(name)
	if err != nil {
		return false, err
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !os.SameFile(opened, info) {
		return false, ErrState
	}
	raw, err := io.ReadAll(io.LimitReader(f, int64(len(content))+1))
	if err != nil || string(raw) != content {
		return false, ErrState
	}
	return true, nil
}
func (s *Service) offline() (bool, error) {
	_, err := s.root.Lstat(maintenance.Marker)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return true, err
	}
	return true, nil
}
func (s *Service) Status(ctx context.Context) (map[string]any, error) {
	done, err := Lease(ctx, s.root, false)
	if err != nil {
		return nil, err
	}
	defer done()
	return s.status(ctx)
}
func (s *Service) status(ctx context.Context) (map[string]any, error) {
	manual, err := s.flag(Manual, "manual\n")
	if err != nil {
		return nil, err
	}
	force, err := s.flag(ForceOpen, "release-failure-override\n")
	if err != nil {
		return nil, err
	}
	offline, err := s.offline()
	if err != nil {
		return nil, err
	}
	agent := release.AgentStatus(ctx, s.agent)
	state, _ := agent["state"].(string)
	phase, _ := agent["phase"].(string)
	blocked := release.Busy(state) || state == "failed" || state == "unavailable"
	source, detail := "open", "站点正常开放"
	enabled := manual || blocked
	switch {
	case offline:
		enabled = true
		source = "native-offline"
		detail = "原生离线维护围栏已关闭，管理员开放操作不能覆盖"
	case force:
		enabled = false
		source = "manual-open-override"
		detail = "管理员已显式结束维护；发布失败遗留维护门禁被覆盖"
	case manual:
		source = "manual"
		detail = "管理员手动进入维护"
	case blocked:
		source = "release"
		detail = "版本发布 " + state + " / " + phase
		if target, ok := agent["target_sha"].(string); ok && release.ValidSHA(target) {
			detail += " · " + target[:12]
		}
	}
	return map[string]any{"maintenance": enabled, "source": source, "detail": detail, "manual": manual, "force_open": force, "release": agent}, nil
}
func (s *Service) Set(ctx context.Context, enabled bool) (map[string]any, error) {
	done, err := Lease(ctx, s.root, true)
	if err != nil {
		return nil, err
	}
	defer done()
	if offline, err := s.offline(); err != nil {
		return nil, err
	} else if offline {
		return nil, errors.New("native offline maintenance cannot be reopened by Admin")
	}
	current := release.AgentStatus(ctx, s.agent)
	state, _ := current["state"].(string)
	if !enabled && (release.Busy(state) || state == "unavailable") {
		return nil, errors.New("release execution or unknown updater state prevents reopening")
	}
	// Validate both existing flags before any mutation; unknown bytes/symlinks
	// are not silently erased to make the requested state appear successful.
	if _, err = s.flag(Manual, "manual\n"); err != nil {
		return nil, err
	}
	if _, err = s.flag(ForceOpen, "release-failure-override\n"); err != nil {
		return nil, err
	}
	if enabled {
		if err = s.remove(ForceOpen); err != nil {
			return nil, err
		}
		if err = s.publish(Manual, "manual\n"); err != nil {
			return nil, err
		}
	} else {
		if err = s.remove(Manual); err != nil {
			return nil, err
		}
		if state == "failed" {
			err = s.publish(ForceOpen, "release-failure-override\n")
		} else {
			err = s.remove(ForceOpen)
		}
		if err != nil {
			return nil, err
		}
	}
	return s.status(ctx)
}
func (s *Service) remove(name string) error {
	if err := s.root.Remove(name); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return fsutil.SyncDirectory(s.root, ".")
}
func (s *Service) publish(name, value string) error {
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return err
	}
	temp := ".site-control-" + hex.EncodeToString(nonce[:])
	f, err := s.root.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
	if err != nil {
		return err
	}
	defer s.root.Remove(temp)
	if err = fsutil.InheritOwner(f, s.root); err == nil {
		_, err = f.WriteString(value)
	}
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
