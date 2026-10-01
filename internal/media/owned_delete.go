package media

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"os"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (s *Service) OwnedDelete(ctx context.Context, relationship, id, name string, expected int64, a store.NodeAudit) error {
	if s.owned == nil {
		return ErrUnavailable
	}
	if !ownedObjectID.MatchString(id) || !managedObject(name, false) || strings.HasPrefix(name, "lyrics/") || expected < 0 {
		return ErrPath
	}
	release, err := s.acquire(ctx, true)
	if err != nil {
		return err
	}
	defer release()
	if err = s.ready(); err != nil {
		return err
	}
	object, err := s.repository.ObjectByID(ctx, id)
	if err != nil {
		return err
	}
	if object == nil {
		// Absence is a receipt only after all possibly live private stages are ruled
		// out. Never delete another ID's file simply because a signed path matches.
		if _, err = s.safeInfo(name); err == nil {
			return os.ErrExist
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		pending, err := s.owned.OwnedPendingUploads(ctx)
		if err != nil {
			return err
		}
		for _, p := range pending {
			if p.MediaID == id || p.Path == name {
				return store.ErrNodeState
			}
		}
		return nil
	}
	if object.Path != name || object.Kind != "audio" && object.Kind != "video" {
		return ErrPath
	}
	info, err := s.safeInfo(name)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Size() != expected {
		return store.ErrNodeState
	}
	random := make([]byte, 16)
	if _, err = rand.Read(random); err != nil {
		return err
	}
	operation := store.DeleteOperation{ID: hex.EncodeToString(random), State: "pending", Items: []store.DeleteItem{{Path: name, Slot: "0", OwnedID: id, Bytes: expected}}}
	if err = s.owned.PrepareOwnedDelete(ctx, relationship, operation, a); err != nil {
		if _, recoverErr := s.reconcileDelete(operation.ID); recoverErr != nil {
			return recoverErr
		}
		return err
	}
	err = s.stageDelete(ctx, operation, func() error {
		_, free, err := fsutil.DiskUsage(s.root)
		if err != nil {
			return err
		}
		return s.owned.CommitOwnedDelete(ctx, operation.ID, free, a)
	})
	verified, recoverErr := s.reconcileDelete(operation.ID)
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
