package media

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path"
	"regexp"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

var ownedObjectID = regexp.MustCompile(`^[a-f0-9]{64}$`)

type StorageReceipt struct {
	ObjectID string `json:"object_id"`
	Bytes    int64  `json:"size_bytes"`
	SHA256   string `json:"sha256"`
	ETag     string `json:"etag"`
}

func ValidateStorageObject(id, name string, size int64) (store.MediaObject, error) {
	if !ownedObjectID.MatchString(id) || !managedObject(name, false) || strings.HasPrefix(name, "lyrics/") || size <= 0 || size > 10*store.GiB {
		return store.MediaObject{}, ErrPath
	}
	kind := "audio"
	if strings.HasPrefix(name, "vido/") {
		kind = "video"
	}
	return store.MediaObject{ID: id, Path: name, Kind: kind}, nil
}
func (s *Service) StorageStat(ctx context.Context, id, name string) (StorageReceipt, error) {
	stream, err := s.OwnedStream(ctx, id)
	if err != nil {
		return StorageReceipt{}, err
	}
	defer stream.File.Close()
	if stream.Path != name {
		return StorageReceipt{}, os.ErrNotExist
	}
	digest := sha256.New()
	size, err := io.CopyBuffer(digest, &contextReader{ctx, stream.File}, make([]byte, 64*1024))
	if err != nil {
		return StorageReceipt{}, err
	}
	hash := hex.EncodeToString(digest.Sum(nil))
	return StorageReceipt{ObjectID: id, Bytes: size, SHA256: hash, ETag: `"` + hash + `"`}, nil
}
func (s *Service) OwnedUpload(ctx context.Context, relationship string, object store.MediaObject, expected int64, reader io.Reader, a store.NodeAudit) (StorageReceipt, error) {
	if s.owned == nil {
		return StorageReceipt{}, ErrUnavailable
	}
	validated, err := ValidateStorageObject(object.ID, object.Path, expected)
	if err != nil || validated.Kind != object.Kind {
		return StorageReceipt{}, ErrPath
	}
	// A lost response can be retried without replacing or charging the original
	// file. Its current digest, not the caller's alleged receipt, is authoritative.
	existing, err := s.repository.ObjectByID(ctx, object.ID)
	if err != nil {
		return StorageReceipt{}, err
	}
	if existing != nil {
		if existing.Path != object.Path || existing.Kind != object.Kind {
			return StorageReceipt{}, os.ErrExist
		}
		receipt, err := s.StorageStat(ctx, object.ID, object.Path)
		if err == nil && receipt.Bytes != expected {
			return StorageReceipt{}, os.ErrExist
		}
		return receipt, err
	}
	operation := ""
	stage, err := s.stage(ctx, reader, expected, func(stage *Stage) error {
		release, err := s.acquire(ctx, true)
		if err != nil {
			return err
		}
		defer release()
		if err = s.ready(); err != nil {
			return err
		}
		if _, err = s.safeInfo(object.Path); err == nil {
			return os.ErrExist
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if err = s.checkLayout(object); err != nil {
			return err
		}
		_, free, err := fsutil.DiskUsage(s.root)
		if err != nil {
			return err
		}
		operation = stage.id
		return s.owned.ReserveOwnedUpload(ctx, operation, relationship, object, expected, free, a)
	})
	if err != nil {
		if operation != "" {
			s.cleanupOwnedReservation(operation)
		}
		return StorageReceipt{}, err
	}
	defer stage.Close()
	if stage.Bytes != expected {
		stage.Close()
		s.cleanupOwnedReservation(operation)
		return StorageReceipt{}, io.ErrUnexpectedEOF
	}
	if !signature(strings.ToLower(path.Ext(object.Path)), stage.Head) {
		stage.Close()
		s.cleanupOwnedReservation(operation)
		return StorageReceipt{}, ErrSignature
	}
	release, err := s.acquire(ctx, true)
	if err != nil {
		stage.Close()
		s.cleanupOwnedReservation(operation)
		return StorageReceipt{}, err
	}
	defer release()
	if err = s.ready(); err != nil {
		return StorageReceipt{}, err
	}
	journal := uploadJournal{Format: "frontiercloud-owned-upload", Version: 1, ID: stage.id, Object: object, Bytes: expected, SHA256: stage.Digest, NodeAudit: a}
	stage.retain = true
	if err = s.writeUploadJournal(journal); err == nil {
		err = s.finishUpload(ctx, journal)
	}
	if err != nil {
		s.markRecovery()
		return StorageReceipt{}, errors.Join(ErrRecovery, err)
	}
	return StorageReceipt{ObjectID: object.ID, Bytes: expected, SHA256: stage.Digest, ETag: `"` + stage.Digest + `"`}, nil
}
func (s *Service) cleanupOwnedReservation(operation string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	release, err := s.acquire(ctx, true)
	if err != nil {
		return
	}
	defer release()
	// Stages are closed by the caller before entering this capacity compensation.
	if _, err = s.root.Lstat(".upload-" + operation + ".part"); !errors.Is(err, os.ErrNotExist) {
		s.markRecovery()
		return
	}
	if err = s.syncDirectory("."); err != nil {
		s.markRecovery()
		return
	}
	if err = s.owned.ReleaseOwnedUpload(ctx, operation, store.NodeAudit{Actor: "upload-cleanup"}); err != nil {
		s.markRecovery()
	}
}

// Called under the startup volume lease after publication intents and dead
// stages are reconciled. A live worker's OS lease prevents premature release.
func (s *Service) recoverOwnedReservations(ctx context.Context) error {
	if s.owned == nil {
		return nil
	}
	pending, err := s.owned.OwnedPendingUploads(ctx)
	if err != nil {
		return err
	}
	for _, v := range pending {
		if !operationID.MatchString(v.ID) {
			return ErrRecovery
		}
		leaseName := ".upload-" + v.ID + ".lease"
		if info, err := s.root.Lstat(leaseName); err == nil {
			if !info.Mode().IsRegular() {
				return ErrRecovery
			}
			f, err := s.root.OpenFile(leaseName, os.O_RDWR, 0)
			if err != nil {
				return err
			}
			release, err := filelease.Try(f, true)
			if errors.Is(err, filelease.ErrBusy) {
				continue
			}
			if err != nil {
				return err
			}
			release()
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		// A surviving publication journal is never discarded as an abandoned stream.
		if _, err := s.root.Lstat(journalPath(v.ID)); err == nil {
			return ErrRecovery
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		stageName := ".upload-" + v.ID + ".part"
		if info, err := s.root.Lstat(stageName); err == nil {
			if !info.Mode().IsRegular() {
				return ErrRecovery
			}
			if err = s.root.Remove(stageName); err != nil {
				return err
			}
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if err = s.syncDirectory("."); err != nil {
			return err
		}
		if err = s.owned.ReleaseOwnedUpload(ctx, v.ID, store.NodeAudit{Actor: "startup-cleanup"}); err != nil {
			return err
		}
	}
	return nil
}
