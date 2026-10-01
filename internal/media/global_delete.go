package media

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type GlobalDeleteResult struct {
	Deleted int      `json:"deleted"`
	Pending []string `json:"pending_delete"`
}

func (s *Service) DeleteGlobal(ctx context.Context, paths []string, a store.AdminAudit) (GlobalDeleteResult, error) {
	result := GlobalDeleteResult{Pending: []string{}}
	if err := s.requireMaster(ctx); err != nil {
		return result, err
	}
	if err := s.Ready(ctx); err != nil {
		return result, err
	}
	items := []store.DeleteItem{}
	for _, name := range paths {
		if strings.HasPrefix(name, "lyrics") {
			return result, ErrPath
		}
		directory := !managedObject(name, false)
		if !managedObject(name, directory) {
			return result, ErrPath
		}
		items = append(items, store.DeleteItem{Path: name, Directory: directory})
	}
	// No filesystem lock is held while a remote node is contacted. SQL owns
	// the durable path/role fence, so every pending row continues to block reuse.
	rows, err := s.pool.PrepareGlobalDelete(ctx, items, a)
	if err != nil {
		return result, err
	}
	for _, v := range rows {
		if err := s.retryGlobalDelete(ctx, v, a); err != nil {
			_ = s.pool.DeferGlobalDelete(ctx, v.ID)
			result.Pending = append(result.Pending, v.Path)
		} else {
			result.Deleted++
		}
	}
	return result, nil
}

func (s *Service) retryGlobalDelete(ctx context.Context, v store.GlobalMedia, a store.AdminAudit) error {
	if v.State != "pending_delete" {
		return store.ErrNodeState
	}
	row, err := s.nodes.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	if row.Role != "Master" {
		return store.ErrNodeState
	}
	if v.MemberID == row.ID {
		return s.deleteMasterLocal(ctx, v, a)
	}
	if s.control == nil || v.RelationshipID == nil {
		return ErrUnavailable
	}
	// This volume lease gives a private cross-worker lock inode, without
	// serializing unrelated media streams or holding the volume during HTTPS.
	release, err := s.globalDeleteLease(v.ID)
	if err != nil {
		return err
	}
	defer release()
	upload := store.UploadReservation{MemberID: v.MemberID, MediaID: v.ObjectID, Path: v.Path, Kind: v.Kind, ExpectedBytes: v.Bytes, Member: store.StorageMember{ID: v.MemberID, RelationshipID: v.RelationshipID}}
	placement, err := s.pool.GlobalPlacement(ctx, v.ID)
	if err != nil {
		return err
	}
	if placement == nil {
		return nil
	}
	if placement.State != "pending_delete" || placement.ObjectID != v.ObjectID || placement.MemberID != v.MemberID || placement.Path != v.Path || placement.Bytes != v.Bytes {
		return store.ErrNodeState
	}
	if err := s.control.DeleteStorage(ctx, upload); err != nil {
		return err
	}
	return s.pool.CompleteGlobalDelete(ctx, v.ID, a)
}

func (s *Service) globalDeleteLease(id string) (func(), error) {
	if !ownedObjectID.MatchString(id) {
		return nil, ErrPath
	}
	// Use a separate namespace from uploads; identical object IDs always map
	// to an identical lock, including restarted background workers.
	return s.privateLease(".global-delete-" + id + ".lease")
}

func (s *Service) deleteMasterLocal(ctx context.Context, v store.GlobalMedia, a store.AdminAudit) error {
	release, err := s.acquire(ctx, true)
	if err != nil {
		return err
	}
	defer release()
	if err = s.ready(); err != nil {
		return err
	}
	placement, err := s.pool.GlobalPlacement(ctx, v.ID)
	if err != nil {
		return err
	}
	if placement == nil {
		return nil
	}
	if placement.State != "pending_delete" || placement.ObjectID != v.ObjectID || placement.MemberID != v.MemberID || placement.Path != v.Path || placement.Bytes != v.Bytes {
		return store.ErrNodeState
	}
	o, err := s.repository.ObjectByID(ctx, v.ObjectID)
	if err != nil {
		return err
	}
	if o == nil || o.Path != v.Path || o.Kind != v.Kind {
		return store.ErrNodeState
	}
	info, err := s.safeInfo(v.Path)
	absent := errors.Is(err, os.ErrNotExist)
	if err != nil && !absent {
		return err
	}
	if !absent && (!info.Mode().IsRegular() || info.Size() != v.Bytes) {
		return store.ErrNodeState
	}
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		return err
	}
	op := store.DeleteOperation{ID: hex.EncodeToString(random), State: "pending", Items: []store.DeleteItem{{Path: v.Path, Slot: "0", OwnedID: v.ObjectID, GlobalID: v.ID, Bytes: v.Bytes, Absent: absent}}}
	if err := s.pool.PrepareMasterDelete(ctx, op, a); err != nil {
		if _, recoverErr := s.reconcileDelete(op.ID); recoverErr != nil {
			return recoverErr
		}
		return err
	}
	err = s.stageDelete(ctx, op, func() error { return s.pool.CommitMasterDelete(ctx, op.ID, a) })
	verified, recoverErr := s.reconcileDelete(op.ID)
	if recoverErr != nil {
		return recoverErr
	}
	if verified == nil {
		s.markRecovery()
		return ErrRecovery
	}
	if err != nil && verified.State != "committed" {
		return err
	}
	return nil
}

// RetryGlobalDeletes leaves unknown physical placements intact. It is safe for
// multiple workers and restarts; only a verified storage receipt refunds quota.
func (s *Service) RetryGlobalDeletes(ctx context.Context) error {
	if s.pool == nil || s.nodes == nil {
		return nil
	}
	rows, err := s.pool.PendingGlobalDeletes(ctx, 50)
	if err != nil {
		return err
	}
	var workers sync.WaitGroup
	limit := make(chan struct{}, 4)
	for _, v := range rows {
		if ctx.Err() != nil {
			break
		}
		select {
		case limit <- struct{}{}:
		case <-ctx.Done():
			workers.Wait()
			return ctx.Err()
		}
		workers.Go(func() {
			defer func() { <-limit }()
			bounded, cancel := context.WithTimeout(ctx, 20*time.Second)
			defer cancel()
			if err := s.retryGlobalDelete(bounded, v, store.AdminAudit{Action: "global-delete-retry", RequestID: "background-delete"}); err != nil {
				// Rotate failed rows behind older pending work, preventing starvation.
				_ = s.pool.DeferGlobalDelete(ctx, v.ID)
				if !errors.Is(err, store.ErrNodeState) && !errors.Is(err, os.ErrNotExist) && !errors.Is(err, ErrUnavailable) {
					slog.Warn("global media deletion deferred", "media_id", v.ID, "error", err)
				}
			}
		})
	}
	workers.Wait()
	return ctx.Err()
}

func (s *Service) RunGlobalDeletes(ctx context.Context) {
	timer := time.NewTicker(30 * time.Second)
	defer timer.Stop()
	for {
		if err := s.RetryGlobalRenames(ctx); err != nil && ctx.Err() == nil {
			slog.Warn("global directory rename retry failed", "error", err)
		}
		if err := s.RetryGlobalDeletes(ctx); err != nil && ctx.Err() == nil {
			slog.Warn("global media deletion retry failed", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
	}
}
