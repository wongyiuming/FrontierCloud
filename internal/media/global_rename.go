package media

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path"
	"sort"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// A SQL intent fences both directory names before any owner is contacted. We
// roll forward rather than guessing whether an offline owner needs rollback.
func (s *Service) RenameGlobal(ctx context.Context, old, target string, a store.AdminAudit) (RenameResult, error) {
	if err := s.requireMaster(ctx); err != nil {
		return RenameResult{}, err
	}
	if !store.ValidDirectoryRename(old, target) {
		return RenameResult{}, ErrPath
	}
	if err := s.Ready(ctx); err != nil {
		return RenameResult{}, err
	}
	op, err := s.pool.PrepareGlobalRename(ctx, old, target, a)
	if err != nil {
		return RenameResult{}, err
	}
	if err := s.retryGlobalRename(ctx, op); err != nil {
		_ = s.pool.DeferGlobalRename(ctx, op.ID)
		return RenameResult{}, errors.Join(ErrUnavailable, err)
	}
	return RenameResult{Status: "renamed", Old: old, New: target}, nil
}

func globalRenameMarker(id string) string { return ".global-rename-" + id + ".marker" }

func (s *Service) retryGlobalRename(ctx context.Context, op store.GlobalRenameOperation) error {
	if !operationID.MatchString(op.ID) {
		return ErrPath
	}
	release, err := s.privateLease(".global-rename-" + op.ID + ".lease")
	if err != nil {
		return err
	}
	defer release()
	current, err := s.pool.GlobalRename(ctx, op.ID)
	if err != nil {
		return err
	}
	if current == nil {
		return store.ErrNodeState
	}
	op = *current
	row, err := s.nodes.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	if row.Role != "Master" || row.ID != op.MasterID {
		return store.ErrNodeState
	}
	if op.State == "rename_done" {
		return s.cleanGlobalRenameMarker(ctx, op)
	}
	if op.State == "rename_cleanup" {
		return s.finishGlobalRenameCleanup(ctx, op)
	}
	groups := map[string][]store.GlobalMedia{}
	for _, v := range op.Media {
		groups[v.MemberID] = append(groups[v.MemberID], v)
	}
	owners := make([]string, 0, len(groups))
	for owner := range groups {
		owners = append(owners, owner)
	}
	sort.Strings(owners)
	for _, owner := range owners {
		if owner == row.ID {
			if err := s.moveGlobalRenameLocal(ctx, op, groups[owner]); err != nil {
				return err
			}
			continue
		}
		if s.control == nil || groups[owner][0].RelationshipID == nil {
			return ErrUnavailable
		}
		// Native Followers replay operation IDs. A legacy Python Follower may
		// have completed its move but lost the reply; authenticated destination
		// stats for ALL exact object IDs provide a non-destructive convergence
		// proof instead of renaming an unknown path back over live bytes.
		moveErr := s.control.RenameStorage(ctx, *groups[owner][0].RelationshipID, op.Old, op.New, op.ID)
		if err := s.verifyGlobalRenameRemote(ctx, op, groups[owner]); err != nil {
			return errors.Join(moveErr, err)
		}
	}
	if err := s.pool.CompleteGlobalRename(ctx, op.ID); err != nil {
		// SQL may have committed while its acknowledgement was lost.
		verified, readErr := s.pool.GlobalRename(ctx, op.ID)
		if readErr != nil || verified == nil || verified.State != "rename_done" && verified.State != "rename_cleanup" {
			return errors.Join(err, readErr)
		}
	}
	return s.finishGlobalRenameCleanup(ctx, op)
}

func (s *Service) finishGlobalRenameCleanup(ctx context.Context, op store.GlobalRenameOperation) error {
	if err := s.cleanGlobalRenameMarker(ctx, op); err != nil {
		return err
	}
	return s.pool.CompleteGlobalRenameCleanup(ctx, op.ID)
}

func (s *Service) verifyGlobalRenameRemote(ctx context.Context, op store.GlobalRenameOperation, values []store.GlobalMedia) error {
	for _, v := range values {
		name := op.New + strings.TrimPrefix(v.Path, op.Old)
		upload := store.UploadReservation{MemberID: v.MemberID, MediaID: v.ObjectID, Path: name, Kind: v.Kind, ExpectedBytes: v.Bytes, Member: store.StorageMember{ID: v.MemberID, RelationshipID: v.RelationshipID}}
		result, err := s.control.StatStorage(ctx, upload)
		if err != nil {
			return err
		}
		id, _ := result["object_id"].(string)
		number, ok := result["size_bytes"].(json.Number)
		bytes, e := number.Int64()
		hash, _ := result["sha256"].(string)
		etag, _ := result["etag"].(string)
		if !ok || e != nil || id != v.ObjectID || bytes != v.Bytes || !ownedObjectID.MatchString(hash) || etag != `"`+hash+`"` {
			return store.ErrNodeState
		}
		// Old imported catalogs use size/mtime ETags. Native upload catalogs
		// additionally bind the original digest; keep that stronger proof.
		original := strings.Trim(v.ETag, `"`)
		if ownedObjectID.MatchString(original) && original != hash {
			return store.ErrNodeState
		}
	}
	return nil
}

func (s *Service) moveGlobalRenameLocal(ctx context.Context, op store.GlobalRenameOperation, values []store.GlobalMedia) error {
	release, err := s.acquire(ctx, true)
	if err != nil {
		return err
	}
	defer release()
	if err := s.ready(); err != nil {
		return err
	}
	old, oldErr := s.safeInfo(op.Old)
	target, targetErr := s.safeInfo(op.New)
	if oldErr != nil && !errors.Is(oldErr, os.ErrNotExist) {
		return oldErr
	}
	if targetErr != nil && !errors.Is(targetErr, os.ErrNotExist) {
		return targetErr
	}
	if oldErr == nil && targetErr == nil {
		return os.ErrExist
	}
	name := op.New
	if oldErr == nil {
		if !old.IsDir() {
			return ErrPath
		}
		name = op.Old
	} else if targetErr != nil || !target.IsDir() {
		return ErrRecovery
	}
	// Local SQL paths are deliberately not rewritten until ALL owners have
	// proved their move. The marker + SQL manifest binds the renamed directory.
	for _, v := range values {
		object, err := s.repository.ObjectByID(ctx, v.ObjectID)
		if err != nil {
			return err
		}
		if object == nil || object.Path != v.Path || object.Kind != v.Kind {
			return store.ErrNodeState
		}
		physical := name + strings.TrimPrefix(v.Path, op.Old)
		info, err := s.safeInfo(physical)
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() || info.Size() != v.Bytes {
			return store.ErrNodeState
		}
		if digest := strings.Trim(v.ETag, `"`); ownedObjectID.MatchString(digest) {
			f, err := s.root.Open(physical)
			if err != nil {
				return err
			}
			hash := sha256.New()
			bytes, copyErr := io.CopyBuffer(hash, &contextReader{ctx, io.LimitReader(f, v.Bytes+1)}, make([]byte, 64*1024))
			f.Close()
			if copyErr != nil {
				return copyErr
			}
			if bytes != v.Bytes || hex.EncodeToString(hash.Sum(nil)) != digest {
				return store.ErrNodeState
			}
		}
	}
	marker := name + "/" + globalRenameMarker(op.ID)
	info, err := s.root.Lstat(marker)
	if oldErr == nil && err == nil && info.Mode().IsRegular() && info.Size() < 32 {
		// A process can die after creating its source marker but before its one
		// small write completes. SQL still owns the source scope and the exact
		// registered files above were verified. Repair ONLY a prefix of this
		// operation's private marker; never adopt/repair a destination marker.
		data, readErr := s.root.ReadFile(marker)
		if readErr != nil || int64(len(data)) != info.Size() || !strings.HasPrefix(op.ID, string(data)) {
			return ErrRecovery
		}
		if err := s.root.Remove(marker); err != nil {
			return err
		}
		if err := s.syncDirectory(name); err != nil {
			return err
		}
		err = os.ErrNotExist
	}
	if errors.Is(err, os.ErrNotExist) && oldErr == nil {
		f, err := s.root.OpenFile(marker, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			return err
		}
		_, err = f.WriteString(op.ID)
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
		if err := s.syncDirectory(name); err != nil {
			return err
		}
	} else {
		if err != nil || !info.Mode().IsRegular() || info.Size() != 32 {
			return ErrRecovery
		}
		data, err := s.root.ReadFile(marker)
		if err != nil || string(data) != op.ID {
			return ErrRecovery
		}
		f, err := s.root.OpenFile(marker, os.O_RDWR, 0)
		if err != nil {
			return err
		}
		err = f.Sync()
		f.Close()
		if err != nil {
			return err
		}
		if err := s.syncDirectory(name); err != nil {
			return err
		}
	}
	if oldErr == nil {
		if err := s.renameExclusive(op.Old, op.New); err != nil {
			return err
		}
		if err := s.syncDirectory(path.Dir(op.New)); err != nil {
			return err
		}
	}
	return nil
}

func (s *Service) cleanGlobalRenameMarker(ctx context.Context, op store.GlobalRenameOperation) error {
	hasLocal := false
	for _, v := range op.Media {
		hasLocal = hasLocal || v.MemberID == op.MasterID
	}
	if !hasLocal {
		return nil
	}
	release, err := s.acquire(ctx, true)
	if err != nil {
		return err
	}
	defer release()
	name := op.New + "/" + globalRenameMarker(op.ID)
	if _, err := s.safeInfo(op.New); err != nil {
		return err
	}
	info, err := s.root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		// An earlier attempt may have unlinked the marker but lost its directory
		// fsync. Persist absence again before releasing the SQL namespace fence.
		return s.syncDirectory(op.New)
	}
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Size() != 32 {
		return ErrRecovery
	}
	data, err := s.root.ReadFile(name)
	if err != nil || string(data) != op.ID {
		return ErrRecovery
	}
	if err := s.root.Remove(name); err != nil {
		return err
	}
	return s.syncDirectory(op.New)
}

func (s *Service) RetryGlobalRenames(ctx context.Context) error {
	if s.nodes == nil || s.pool == nil {
		return nil
	}
	operations, err := s.pool.PendingGlobalRenames(ctx, 10)
	if err != nil {
		return err
	}
	for _, op := range operations {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		bounded, cancel := context.WithTimeout(ctx, 20*time.Second)
		err := s.retryGlobalRename(bounded, op)
		cancel()
		if err != nil {
			_ = s.pool.DeferGlobalRename(ctx, op.ID)
		}
	}
	return ctx.Err()
}
