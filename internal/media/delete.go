package media

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

var ErrRecovery = errors.New("media transaction requires recovery")
var operationID = regexp.MustCompile(`^[0-9a-f]{32}$`)
var slotID = regexp.MustCompile(`^(0|[1-9][0-9]{0,5})$`)

func (s *Service) ready() error {
	if s.recoveryRequired {
		return ErrRecovery
	}
	if _, err := s.root.Lstat(".recovery-required"); err == nil {
		return ErrRecovery
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	// A worker killed before reporting an error cannot set the shared flag.
	// Durable journals are themselves evidence that traffic must wait for
	// recovery, including the gap after a delete prepare but before staging.
	f, err := s.root.Open(".")
	if err != nil {
		return err
	}
	entries, err := f.ReadDir(-1)
	f.Close()
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if uploadJournalName.MatchString(entry.Name()) || renameJournalName.MatchString(entry.Name()) {
			return ErrRecovery
		}
	}
	check, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	operations, err := s.repository.DeleteOperations(check)
	if err != nil {
		return err
	}
	for _, operation := range operations {
		if operation.State != "committed" {
			return ErrRecovery
		}
	}
	return nil
}

func (s *Service) Ready(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return leaseErr
	}
	defer release()
	if err := ctx.Err(); err != nil {
		return err
	}
	return s.ready()
}
func managedObject(name string, directory bool) bool {
	if name == defaultLyric || name == "lyrics" || name != path.Clean(name) || strings.ContainsAny(name, "\\\x00") {
		return false
	}
	parts := strings.Split(name, "/")
	for _, part := range parts {
		if part == "" || strings.HasPrefix(part, ".") || strings.Contains(part, ":") {
			return false
		}
	}
	if parts[0] != "music" && parts[0] != "vido" && parts[0] != "lyrics" {
		return false
	}
	if directory {
		return len(parts) <= 3
	}
	return len(parts) >= 2 && len(parts) <= 4 && (parts[0] == "lyrics" || len(parts) >= 3) && validExt(parts[0], name)
}

func validateOperation(operation store.DeleteOperation) error {
	if !operationID.MatchString(operation.ID) || (operation.State != "pending" && operation.State != "committed") || len(operation.Items) == 0 || len(operation.Items) > 5000 {
		return ErrRecovery
	}
	seen := map[string]bool{}
	names := map[string]bool{}
	for _, item := range operation.Items {
		if !slotID.MatchString(item.Slot) || seen[item.Slot] || names[item.Path] || !managedObject(item.Path, item.Directory) {
			return ErrRecovery
		}
		if item.OwnedID != "" && (!ownedObjectID.MatchString(item.OwnedID) || item.Directory || item.Bytes < 0 || len(operation.Items) != 1) {
			return ErrRecovery
		}
		if item.GlobalID != "" && (!ownedObjectID.MatchString(item.GlobalID) || item.OwnedID == "") {
			return ErrRecovery
		}
		if item.Absent && (item.OwnedID == "" || item.Directory) {
			return ErrRecovery
		}
		seen[item.Slot] = true
		names[item.Path] = true
	}
	for _, a := range operation.Items {
		for _, b := range operation.Items {
			if a.Path != b.Path && strings.HasPrefix(a.Path, b.Path+"/") {
				return ErrRecovery
			}
		}
	}
	return nil
}

func (s *Service) syncDirectory(name string) error {
	if runtime.GOOS == "windows" {
		return nil
	}
	f, err := s.root.Open(name)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}
func (s *Service) quarantine(operation store.DeleteOperation) (string, error) {
	name := ".delete-" + operation.ID
	info, err := s.root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return name, nil
	}
	if err != nil {
		return "", err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", ErrRecovery
	}
	f, err := s.root.Open(name)
	if err != nil {
		return "", err
	}
	entries, err := f.ReadDir(-1)
	f.Close()
	if err != nil {
		return "", err
	}
	slots := map[string]bool{}
	for _, item := range operation.Items {
		slots[item.Slot] = true
	}
	for _, entry := range entries {
		if !slots[entry.Name()] || entry.Type()&os.ModeSymlink != 0 {
			return "", ErrRecovery
		}
	}
	return name, nil
}

func (s *Service) finishDelete(ctx context.Context, operation store.DeleteOperation) error {
	if err := validateOperation(operation); err != nil {
		return err
	}
	quarantine, err := s.quarantine(operation)
	if err != nil {
		return err
	}
	if operation.State == "pending" {
		for i := len(operation.Items) - 1; i >= 0; i-- {
			if err := ctx.Err(); err != nil {
				return err
			}
			item := operation.Items[i]
			if item.Absent {
				if _, err := s.safeInfo(item.Path); !errors.Is(err, os.ErrNotExist) {
					return ErrRecovery
				}
				continue
			}
			staged := quarantine + "/" + item.Slot
			info, stagedErr := s.root.Lstat(staged)
			if stagedErr != nil && !errors.Is(stagedErr, os.ErrNotExist) {
				return stagedErr
			}
			if stagedErr == nil && (info.Mode()&os.ModeSymlink != 0 || info.IsDir() != item.Directory || (!info.IsDir() && !info.Mode().IsRegular())) {
				return ErrRecovery
			}
			original, originalErr := s.safeInfo(item.Path)
			if originalErr != nil && !errors.Is(originalErr, os.ErrNotExist) {
				return originalErr
			}
			if originalErr == nil {
				if stagedErr == nil || original.IsDir() != item.Directory {
					return ErrRecovery
				}
				continue
			}
			if stagedErr != nil {
				return ErrRecovery
			}
			parent := path.Dir(item.Path)
			if parent != "." {
				parentInfo, err := s.safeInfo(parent)
				if err != nil || !parentInfo.IsDir() {
					return ErrRecovery
				}
			}
			if err := s.root.Rename(staged, item.Path); err != nil {
				return err
			}
			if err := s.syncDirectory(parent); err != nil {
				return err
			}
			if err := s.syncDirectory(quarantine); err != nil {
				return err
			}
		}
	}
	// Only this validated operation's private quarantine is recursively removed.
	// os.Root.RemoveAll does not follow descendant symlinks outside the root.
	if err := s.root.RemoveAll(quarantine); err != nil {
		return err
	}
	if err := s.syncDirectory("."); err != nil {
		return err
	}
	return s.repository.ForgetDelete(ctx, operation.ID)
}

func (s *Service) recoverDeletes(ctx context.Context) error {
	operations, err := s.repository.DeleteOperations(ctx)
	if err != nil {
		return err
	}
	for _, operation := range operations {
		if err := s.finishDelete(ctx, operation); err != nil {
			return fmt.Errorf("recover delete %s: %w", operation.ID, err)
		}
	}
	s.recoveryRequired = false
	return nil
}

func (s *Service) reconcileDelete(id string) (*store.DeleteOperation, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	operation, err := s.repository.DeleteOperation(ctx, id)
	if err != nil {
		s.markRecovery()
		return nil, fmt.Errorf("%w: %v", ErrRecovery, err)
	}
	if operation == nil {
		return nil, nil
	}
	if err := s.finishDelete(ctx, *operation); err != nil {
		if operation.State != "committed" {
			s.markRecovery()
			return operation, fmt.Errorf("%w: %v", ErrRecovery, err)
		}
		slog.Error("committed media delete cleanup deferred", "operation", id, "error", err)
	}
	return operation, nil
}

func (s *Service) Delete(ctx context.Context, paths []string, audit store.AdminAudit) (int, error) {
	role, err := s.role(ctx)
	if err != nil {
		return 0, err
	}
	if role == "Follower" {
		return 0, store.ErrNodeState
	}
	if role == "Master" {
		for _, p := range paths {
			if !strings.HasPrefix(p, "lyrics/") {
				return 0, store.ErrNodeState
			}
		}
	}
	release, leaseErr := s.acquire(ctx, true)
	if leaseErr != nil {
		return 0, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return 0, err
	}
	items := []store.DeleteItem{}
	seen := map[string]bool{}
	for _, name := range paths {
		name = strings.TrimSpace(strings.ReplaceAll(name, "\\", "/"))
		info, err := s.safeInfo(name)
		if err != nil {
			return 0, err
		}
		if !managedObject(name, info.IsDir()) || !info.IsDir() && !info.Mode().IsRegular() {
			return 0, ErrPath
		}
		if seen[name] {
			continue
		}
		seen[name] = true
		items = append(items, store.DeleteItem{Path: name, Directory: info.IsDir()})
	}
	if len(items) == 0 {
		return 0, ErrPath
	}
	sort.SliceStable(items, func(i, j int) bool {
		return len(strings.Split(items[i].Path, "/")) < len(strings.Split(items[j].Path, "/"))
	})
	selected := []store.DeleteItem{}
	for _, item := range items {
		covered := false
		for _, parent := range selected {
			if strings.HasPrefix(item.Path, parent.Path+"/") {
				covered = true
				break
			}
		}
		if !covered {
			item.Slot = strconv.Itoa(len(selected))
			selected = append(selected, item)
		}
	}
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		return 0, err
	}
	operation := store.DeleteOperation{ID: hex.EncodeToString(random), State: "pending", Items: selected}
	if err := s.repository.PrepareDelete(ctx, operation, audit); err != nil {
		if _, recoveryErr := s.reconcileDelete(operation.ID); recoveryErr != nil {
			return 0, recoveryErr
		}
		return 0, err
	}
	err = s.stageDelete(ctx, operation, func() error { return s.repository.CommitDelete(ctx, operation.ID, audit) })
	verified, recoveryErr := s.reconcileDelete(operation.ID)
	if recoveryErr != nil {
		return 0, recoveryErr
	}
	if verified == nil {
		s.markRecovery()
		return 0, ErrRecovery
	}
	if err != nil && (verified == nil || verified.State != "committed") {
		return 0, err
	}
	return len(selected), nil
}

// Only the filesystem phase is shared. Native owned deletion has its own
// quota-aware SQL transaction, and cannot use standalone metadata deletion.
func (s *Service) stageDelete(ctx context.Context, operation store.DeleteOperation, commit func() error) error {
	quarantine := ".delete-" + operation.ID
	if err := s.root.Mkdir(quarantine, 0700); err != nil {
		return err
	}
	if err := s.syncDirectory("."); err != nil {
		return err
	}
	for _, item := range operation.Items {
		if err := ctx.Err(); err != nil {
			return err
		}
		if item.Absent {
			if _, err := s.safeInfo(item.Path); !errors.Is(err, os.ErrNotExist) {
				return ErrRecovery
			}
			continue
		}
		if _, err := s.safeInfo(item.Path); err != nil {
			return err
		}
		if err := s.root.Rename(item.Path, quarantine+"/"+item.Slot); err != nil {
			return err
		}
		if err := s.syncDirectory(path.Dir(item.Path)); err != nil {
			return err
		}
		if err := s.syncDirectory(quarantine); err != nil {
			return err
		}
	}
	return commit()
}
