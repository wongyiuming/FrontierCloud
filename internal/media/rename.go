package media

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type RenameResult struct {
	Status string `json:"status"`
	Old    string `json:"old_path"`
	New    string `json:"new_path"`
}
type renameJournal struct {
	Format  string           `json:"format"`
	Version int              `json:"version"`
	ID      string           `json:"id"`
	Old     string           `json:"old_path"`
	New     string           `json:"new_path"`
	Audit   store.AdminAudit `json:"audit"`
}

var renameJournalName = regexp.MustCompile(`^\.rename-[0-9a-f]{32}\.json$`)

func renameDirectoryPath(name string) bool {
	parts := strings.Split(name, "/")
	return managedObject(name, true) && (parts[0] == "music" || parts[0] == "vido") && len(parts) >= 2 && len(parts) <= 3 && utf8.ValidString(name) && utf8.RuneCountInString(name) <= 1024
}
func renameJournalPath(id string) string { return ".rename-" + id + ".json" }
func renameMarker(id string) string      { return ".rename-" + id + ".marker" }
func (s *Service) writeRenameJournal(j renameJournal) error {
	data, err := json.Marshal(j)
	if err != nil {
		return err
	}
	name := renameJournalPath(j.ID)
	f, err := s.root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, err = f.Write(data)
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return s.syncDirectory(".")
}
func (s *Service) finishRename(ctx context.Context, j renameJournal) error {
	if j.Format != "frontiercloud-local-rename" || j.Version != 1 || !operationID.MatchString(j.ID) || !renameDirectoryPath(j.Old) || !renameDirectoryPath(j.New) || j.Old == j.New || path.Dir(j.Old) != path.Dir(j.New) || j.Audit.Action != "directory_rename" {
		return ErrRecovery
	}
	old, oldErr := s.safeInfo(j.Old)
	target, targetErr := s.safeInfo(j.New)
	if oldErr != nil && !errors.Is(oldErr, os.ErrNotExist) {
		return oldErr
	}
	if targetErr != nil && !errors.Is(targetErr, os.ErrNotExist) {
		return targetErr
	}
	if oldErr == nil && targetErr == nil {
		return os.ErrExist
	}
	name := j.New
	if oldErr == nil {
		if !old.IsDir() {
			return ErrRecovery
		}
		name = j.Old
	} else if targetErr != nil || !target.IsDir() {
		return ErrRecovery
	}
	marker := name + "/" + renameMarker(j.ID)
	info, err := s.root.Lstat(marker)
	if err != nil || !info.Mode().IsRegular() || info.Size() != 32 {
		return ErrRecovery
	}
	data, err := s.root.ReadFile(marker)
	if err != nil || string(data) != j.ID {
		return ErrRecovery
	}
	if oldErr == nil {
		if err := s.repository.CheckRename(ctx, j.New); err != nil {
			return err
		}
		if err := s.renameExclusive(j.Old, j.New); err != nil {
			return err
		}
		if err := s.syncDirectory(path.Dir(j.New)); err != nil {
			return err
		}
	}
	if err := s.repository.CompleteRename(ctx, j.Old, j.New, j.ID, j.Audit); err != nil {
		return err
	}
	// Clear the replay intent durably before removing its ownership marker.
	if err := s.root.Remove(renameJournalPath(j.ID)); err != nil {
		return err
	}
	if err := s.syncDirectory("."); err != nil {
		return err
	}
	if err := s.root.Remove(j.New + "/" + renameMarker(j.ID)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return s.syncDirectory(j.New)
}
func (s *Service) Rename(ctx context.Context, old, newName string, audit store.AdminAudit) (RenameResult, error) {
	release, leaseErr := s.acquire(ctx, true)
	if leaseErr != nil {
		return RenameResult{}, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return RenameResult{}, err
	}
	old = strings.TrimSpace(strings.ReplaceAll(old, "\\", "/"))
	newName = strings.TrimSpace(newName)
	if !renameDirectoryPath(old) || newName == "" || strings.HasPrefix(newName, ".") || strings.ContainsAny(newName, "/\\:\x00") || !utf8.ValidString(newName) || len(newName) > 255 || utf8.RuneCountInString(newName) > 255 {
		return RenameResult{}, ErrPath
	}
	target := path.Dir(old) + "/" + newName
	if !renameDirectoryPath(target) {
		return RenameResult{}, ErrPath
	}
	info, err := s.safeInfo(old)
	if err != nil {
		return RenameResult{}, err
	}
	if !info.IsDir() {
		return RenameResult{}, ErrPath
	}
	result := RenameResult{Status: "renamed", Old: old, New: target}
	if old == target {
		result.Status = "unchanged"
		return result, nil
	}
	if _, err := s.safeInfo(target); err == nil {
		return RenameResult{}, os.ErrExist
	} else if !errors.Is(err, os.ErrNotExist) {
		return RenameResult{}, err
	}
	if err := s.repository.CheckRename(ctx, target); err != nil {
		return RenameResult{}, err
	}
	if err := ctx.Err(); err != nil {
		return RenameResult{}, err
	}
	bytes := make([]byte, 16)
	if _, err := rand.Read(bytes); err != nil {
		return RenameResult{}, err
	}
	id := hex.EncodeToString(bytes)
	marker := old + "/" + renameMarker(id)
	f, err := s.root.OpenFile(marker, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return RenameResult{}, err
	}
	_, err = f.WriteString(id)
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = s.syncDirectory(old)
	}
	if err != nil {
		s.root.Remove(marker)
		return RenameResult{}, err
	}
	audit.Action = "directory_rename"
	audit.SourceSummary = ""
	audit.Detail = ""
	audit.UserAgent = limited(audit.UserAgent, 512)
	j := renameJournal{Format: "frontiercloud-local-rename", Version: 1, ID: id, Old: old, New: target, Audit: audit}
	if err := s.writeRenameJournal(j); err != nil {
		s.markRecovery()
		return RenameResult{}, fmt.Errorf("%w: %v", ErrRecovery, err)
	}
	if err := s.finishRename(ctx, j); err != nil {
		s.markRecovery()
		return RenameResult{}, fmt.Errorf("%w: %v", ErrRecovery, err)
	}
	return result, nil
}
func (s *Service) recoverRenames(ctx context.Context) error {
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
		if !renameJournalName.MatchString(entry.Name()) {
			continue
		}
		if !entry.Type().IsRegular() {
			return ErrRecovery
		}
		f, err := s.root.Open(entry.Name())
		if err != nil {
			return err
		}
		decoder := json.NewDecoder(io.LimitReader(f, 64*1024+1))
		decoder.DisallowUnknownFields()
		var j renameJournal
		err = decoder.Decode(&j)
		var extra any
		trailing := decoder.Decode(&extra)
		f.Close()
		if err != nil || trailing != io.EOF || entry.Name() != renameJournalPath(j.ID) {
			return ErrRecovery
		}
		if err := s.finishRename(ctx, j); err != nil {
			return fmt.Errorf("recover directory rename: %w", err)
		}
	}
	return nil
}
