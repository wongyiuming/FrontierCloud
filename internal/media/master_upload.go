package media

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type UploadTicket struct {
	ID        string `json:"upload_id"`
	MediaID   string `json:"media_id"`
	Path      string `json:"path"`
	Transport string `json:"transport"`
	MemberID  string `json:"member_id"`
	URL       string `json:"upload_url"`
	Site      string `json:"site_type"`
	Label     string `json:"site_label"`
}
type UploadResult struct {
	Path    string `json:"path"`
	MediaID string `json:"media_id"`
}

func (s *Service) StoragePool(ctx context.Context) (map[string]any, error) {
	role, err := s.role(ctx)
	if err != nil {
		return nil, err
	}
	if role != "Master" {
		return map[string]any{"members": []store.StorageMember{}, "standalone": true}, nil
	}
	release, err := s.acquire(ctx, false)
	if err != nil {
		return nil, err
	}
	defer release()
	if err = s.ready(); err != nil {
		return nil, err
	}
	members, err := s.pool.Members(ctx)
	if err != nil {
		return nil, err
	}
	total, free, err := fsutil.DiskUsage(s.root)
	if err != nil {
		return nil, err
	}
	result := map[string]any{}
	var allocated, used, reserved, available, offline, largest int64
	for i := range members {
		v := &members[i]
		if v.Kind == "MasterLocal" {
			v.PhysicalFree, v.PhysicalTotal = free, total
			v.Available = 0
			if v.Enabled != 0 && v.Writable != 0 && v.Health == "online" {
				v.Available = min(max(0, v.Allocation-v.Used-v.Reserved), max(0, free-store.PhysicalReserve-v.Reserved))
			}
			v.OnlineWritable = v.Available
		}
		if v.Enabled != 0 {
			allocated += v.Allocation
		}
		used += v.Used
		reserved += v.Reserved
		available += v.Available
		offline += v.OfflineStored
		largest = max(largest, v.Available)
	}
	result["allocated_bytes"], result["used_bytes"], result["reserved_bytes"], result["available_bytes"], result["online_writable_bytes"], result["offline_stored_bytes"] = allocated, used, reserved, available, available, offline
	auto := store.StorageMember{ID: "auto", Kind: "Auto", Transport: "Automatic", Enabled: 1, Health: "online", Writable: 1, Available: largest, OnlineWritable: largest, Compute: map[string]any{}, Backup: map[string]any{}}
	result["members"] = append([]store.StorageMember{auto}, members...)
	return result, nil
}

func (s *Service) BusinessRole(ctx context.Context) (string, error) { return s.role(ctx) }
func (s *Service) requireMaster(ctx context.Context) error {
	role, err := s.role(ctx)
	if err != nil {
		return err
	}
	if role != "Master" || s.pool == nil {
		return store.ErrNodeState
	}
	return nil
}
func (s *Service) ReserveMasterUpload(ctx context.Context, filename, target, relative, site string, size int64, maxName int, a store.AdminAudit) (UploadTicket, error) {
	if err := s.requireMaster(ctx); err != nil {
		return UploadTicket{}, err
	}
	release, err := s.acquire(ctx, true)
	if err != nil {
		return UploadTicket{}, err
	}
	defer release()
	if err = s.ready(); err != nil {
		return UploadTicket{}, err
	}
	o, err := s.uploadDestination(filename, target, relative, false, maxName, true)
	if err != nil {
		return UploadTicket{}, err
	}
	_, free, err := fsutil.DiskUsage(s.root)
	if err != nil {
		return UploadTicket{}, err
	}
	v, err := s.pool.ReserveUpload(ctx, o.Path, site, size, free, a)
	if err != nil {
		return UploadTicket{}, err
	}
	ticket := UploadTicket{ID: v.ID, MediaID: v.MediaID, Path: v.Path, Transport: v.Member.Transport, MemberID: v.MemberID, URL: "/api/v1/media/admin/upload/session/" + v.ID + "/bytes", Site: site, Label: map[string]string{"primary": "主站", "direct": "直连", "relay": "中继"}[site]}
	if v.Member.Transport == "Direct" {
		if s.control == nil {
			return UploadTicket{}, ErrUnavailable
		}
		token, err := s.control.StorageCapability(ctx, v, "upload")
		if err != nil {
			return UploadTicket{}, err
		}
		origin, err := s.control.StorageOrigin(ctx, v)
		if err != nil {
			return UploadTicket{}, err
		}
		ticket.URL = origin + "/internal/v1/storage/" + v.MediaID + "?token=" + token
	}
	return ticket, nil
}

// A fixed, private OS lease serializes upload, finalize and cancel for one
// session across workers. Never unlink it: that would create two lock inodes.
func (s *Service) uploadSessionLease(id string) (func(), error) {
	if !operationID.MatchString(id) {
		return nil, ErrPath
	}
	name := ".session-" + id + ".lease"
	if info, err := s.root.Lstat(name); err == nil && !info.Mode().IsRegular() {
		return nil, ErrPath
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	f, err := s.root.OpenFile(name, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	release, err := filelease.Try(f, true)
	if errors.Is(err, filelease.ErrBusy) {
		return nil, store.ErrNodeState
	}
	return release, err
}
func (s *Service) masterUpload(ctx context.Context, id string) (store.UploadReservation, error) {
	if err := s.requireMaster(ctx); err != nil {
		return store.UploadReservation{}, err
	}
	if err := s.Ready(ctx); err != nil {
		return store.UploadReservation{}, err
	}
	return s.pool.Upload(ctx, id)
}
func completedResult(v store.UploadReservation) UploadResult {
	return UploadResult{Path: v.Path, MediaID: v.MediaID}
}
func checkReceipt(v store.UploadReservation, value map[string]any) (StorageReceipt, error) {
	id, _ := value["object_id"].(string)
	digest, _ := value["sha256"].(string)
	etag, _ := value["etag"].(string)
	number, ok := value["size_bytes"].(json.Number)
	size, err := number.Int64()
	if !ok || err != nil || size != v.ExpectedBytes || id != v.MediaID || !ownedObjectID.MatchString(digest) || etag != `"`+digest+`"` {
		return StorageReceipt{}, store.ErrNodeState
	}
	return StorageReceipt{ObjectID: id, Bytes: size, SHA256: digest, ETag: etag}, nil
}
func (s *Service) UploadMasterBytes(ctx context.Context, id string, reader io.Reader, a store.AdminAudit) (UploadResult, error) {
	release, err := s.uploadSessionLease(id)
	if err != nil {
		return UploadResult{}, err
	}
	defer release()
	v, err := s.masterUpload(ctx, id)
	if err != nil {
		return UploadResult{}, err
	}
	if v.State == "complete" {
		return completedResult(v), nil
	}
	if v.State != "reserved" || v.ExpiresAt <= time.Now().Unix() {
		return UploadResult{}, store.ErrNodeState
	}
	if v.Member.Kind != "MasterLocal" {
		if s.control == nil {
			return UploadResult{}, ErrUnavailable
		}
		value, err := s.control.UploadStorage(ctx, v, &contextReader{ctx, reader})
		if err != nil {
			return UploadResult{}, err
		}
		receipt, err := checkReceipt(v, value)
		if err != nil {
			return UploadResult{}, err
		}
		if _, err = s.pool.FinalizeUpload(ctx, v.ID, receipt.ObjectID, receipt.Bytes, receipt.ETag, a); err != nil {
			return UploadResult{}, err
		}
		return completedResult(v), nil
	}
	stage, err := s.Stage(ctx, reader, v.ExpectedBytes)
	if err != nil {
		return UploadResult{}, err
	}
	defer stage.Close()
	if stage.Bytes != v.ExpectedBytes {
		return UploadResult{}, io.ErrUnexpectedEOF
	}
	if !signature(strings.ToLower(path.Ext(v.Path)), stage.Head) {
		return UploadResult{}, ErrSignature
	}
	volumeRelease, err := s.acquire(ctx, true)
	if err != nil {
		return UploadResult{}, err
	}
	defer volumeRelease()
	if err = s.ready(); err != nil {
		return UploadResult{}, err
	}
	if _, err = s.safeInfo(v.Path); err == nil {
		return UploadResult{}, os.ErrExist
	} else if !errors.Is(err, os.ErrNotExist) {
		return UploadResult{}, err
	}
	journal := uploadJournal{Format: "frontiercloud-master-upload", Version: 1, ID: stage.id, UploadSessionID: v.ID, Object: store.MediaObject{ID: v.MediaID, Path: v.Path, Kind: v.Kind}, Bytes: stage.Bytes, SHA256: stage.Digest, Audit: a}
	stage.retain = true
	if err = s.writeUploadJournal(journal); err == nil {
		err = s.finishUpload(ctx, journal)
	}
	if err != nil {
		s.markRecovery()
		return UploadResult{}, errors.Join(ErrRecovery, err)
	}
	return completedResult(v), nil
}
func (s *Service) FinalizeMasterUpload(ctx context.Context, id string, a store.AdminAudit) (UploadResult, error) {
	release, err := s.uploadSessionLease(id)
	if err != nil {
		return UploadResult{}, err
	}
	defer release()
	v, err := s.masterUpload(ctx, id)
	if err != nil {
		return UploadResult{}, err
	}
	if v.State == "complete" {
		return completedResult(v), nil
	}
	if v.Member.Kind == "MasterLocal" || s.control == nil || v.State != "reserved" || v.ExpiresAt <= time.Now().Unix() {
		return UploadResult{}, store.ErrNodeState
	}
	value, err := s.control.StatStorage(ctx, v)
	if err != nil {
		return UploadResult{}, err
	}
	receipt, err := checkReceipt(v, value)
	if err != nil {
		return UploadResult{}, err
	}
	if _, err = s.pool.FinalizeUpload(ctx, id, receipt.ObjectID, receipt.Bytes, receipt.ETag, a); err != nil {
		return UploadResult{}, err
	}
	return completedResult(v), nil
}
func (s *Service) CancelMasterUpload(ctx context.Context, id string, a store.AdminAudit) error {
	release, err := s.uploadSessionLease(id)
	if err != nil {
		return err
	}
	defer release()
	v, err := s.masterUpload(ctx, id)
	if err != nil {
		return err
	}
	if v.State == "complete" {
		return nil
	}
	if v.State != "reserved" {
		return store.ErrNodeState
	}
	if v.Member.Kind == "MasterLocal" {
		volumeRelease, err := s.acquire(ctx, true)
		if err != nil {
			return err
		}
		defer volumeRelease()
		if err = s.ready(); err != nil {
			return err
		}
		// A committed object or unknown file is never deleted by cancellation.
		object, err := s.repository.ObjectByID(ctx, v.MediaID)
		if err != nil {
			return err
		}
		if object != nil {
			return store.ErrNodeState
		}
		if _, err = s.safeInfo(v.Path); !errors.Is(err, os.ErrNotExist) {
			if err == nil {
				return os.ErrExist
			}
			return err
		}
		if err = s.syncDirectory("."); err != nil {
			return err
		}
	} else {
		if s.control == nil {
			return ErrUnavailable
		}
		if err = s.control.DeleteStorage(ctx, v); err != nil {
			return err
		}
	}
	return s.pool.ReleaseCleanedUpload(ctx, id, a)
}
