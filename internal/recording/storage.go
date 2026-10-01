package recording

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

var ErrRecovery = errors.New("recording storage requires recovery")
var ErrSize = errors.New("录音上传字节数与预留大小不一致")
var identifier = regexp.MustCompile(`^[a-f0-9]{32}$`)
var journalName = regexp.MustCompile(`^\.recording-([a-f0-9]{32})\.json$`)
var stageName = regexp.MustCompile(`^\.recording-([a-f0-9]{32})\.part$`)
var temporaryName = regexp.MustCompile(`^\.recording-([a-f0-9]{32})\.tmp$`)

type Storage struct {
	root     *os.Root
	repo     store.RecordingRepository
	nodes    store.NodeRepository
	mutation sync.RWMutex
}
type journal struct {
	Version      int             `json:"version"`
	Operation    string          `json:"operation"`
	Relationship string          `json:"relationship"`
	Recording    store.Recording `json:"recording"`
	// User/member/state are not public Recording JSON fields.
	UserID    string                 `json:"user_id"`
	MemberID  string                 `json:"member_id"`
	Receipt   store.RecordingReceipt `json:"receipt"`
	Audit     store.KaraokeAudit     `json:"audit"`
	NodeAudit store.NodeAudit        `json:"node_audit"`
	Absent    bool                   `json:"physical_absent"`
}

func New(root *os.Root, repo store.RecordingRepository, nodes store.NodeRepository) (*Storage, error) {
	s := &Storage{root: root, repo: repo, nodes: nodes}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if e := s.Recover(ctx); e != nil {
		return nil, e
	}
	return s, nil
}
func (s *Storage) Free() (int64, error) { _, free, e := fsutil.DiskUsage(s.root); return free, e }
func Path(relationship, user, id string) (string, error) {
	if !identifier.MatchString(relationship) || !identifier.MatchString(user) || !identifier.MatchString(id) {
		return "", store.ErrRecordingState
	}
	return relationship + "/" + user + "/" + id + ".bin", nil
}
func (s *Storage) syncDir(name string) error { return fsutil.SyncDirectory(s.root, name) }
func (s *Storage) info(name string) (os.FileInfo, error) {
	parts := strings.Split(name, "/")
	for i := range parts {
		info, e := s.root.Lstat(strings.Join(parts[:i+1], "/"))
		if e != nil {
			return nil, e
		}
		if info.Mode()&os.ModeSymlink != 0 || i < len(parts)-1 && !info.IsDir() || i == len(parts)-1 && !info.Mode().IsRegular() {
			return nil, ErrRecovery
		}
		if i == len(parts)-1 {
			return info, nil
		}
	}
	return nil, ErrRecovery
}
func (s *Storage) acquire(ctx context.Context, write bool) (func(), error) {
	for {
		if e := ctx.Err(); e != nil {
			return nil, e
		}
		locked := s.mutation.TryRLock
		if write {
			locked = s.mutation.TryLock
		}
		if locked() {
			break
		}
		timer := time.NewTimer(25 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
	local := s.mutation.RUnlock
	if write {
		local = s.mutation.Unlock
	}
	file, e := s.privateFile(".recordings-mutation.lock", os.O_CREATE|os.O_RDWR)
	if e != nil {
		local()
		return nil, e
	}
	release, e := filelease.Acquire(ctx, file, write)
	if e != nil {
		local()
		return nil, e
	}
	return func() { release(); local() }, nil
}
func (s *Storage) privateFile(name string, flags int) (*os.File, error) {
	info, e := s.root.Lstat(name)
	if e == nil && !info.Mode().IsRegular() {
		return nil, ErrRecovery
	}
	if e != nil && !errors.Is(e, os.ErrNotExist) {
		return nil, e
	}
	return s.root.OpenFile(name, flags, 0600)
}
func (s *Storage) Lock(id string) (func(), error) {
	if !identifier.MatchString(id) {
		return nil, store.ErrRecordingState
	}
	file, e := s.privateFile(".recording-"+id+".lease", os.O_CREATE|os.O_RDWR)
	if e != nil {
		return nil, e
	}
	return filelease.Try(file, true)
}
func (s *Storage) ready() error {
	if _, e := s.root.Lstat(".recordings-recovery-required"); e == nil {
		return ErrRecovery
	} else if !errors.Is(e, os.ErrNotExist) {
		return e
	}
	return nil
}
func (s *Storage) Ready(ctx context.Context) error {
	release, e := s.acquire(ctx, false)
	if e != nil {
		return e
	}
	defer release()
	return s.ready()
}
func (s *Storage) fence() {
	file, e := s.privateFile(".recordings-recovery-required", os.O_CREATE|os.O_WRONLY)
	if e == nil {
		file.Sync()
		file.Close()
		s.syncDir(".")
	}
}
func (s *Storage) remove(name string) error {
	e := s.root.Remove(name)
	if errors.Is(e, os.ErrNotExist) {
		return nil
	}
	return e
}
func (s *Storage) makeParents(name string) error {
	current := ""
	for _, part := range strings.Split(path.Dir(name), "/") {
		if current != "" {
			current += "/"
		}
		current += part
		info, e := s.root.Lstat(current)
		if errors.Is(e, os.ErrNotExist) {
			if e = s.root.Mkdir(current, 0750); e != nil {
				return e
			}
			if e = s.syncDir(path.Dir(current)); e != nil {
				return e
			}
			info, e = s.root.Lstat(current)
		}
		if e != nil {
			return e
		}
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return ErrRecovery
		}
	}
	return nil
}
func (s *Storage) writeJournal(j journal) error {
	raw, e := json.Marshal(j)
	if e != nil {
		return e
	}
	if len(raw) > store.MaxRecordingMetadata+16384 {
		return ErrRecovery
	}
	name := ".recording-" + j.Recording.ID + ".tmp"
	file, e := s.privateFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY)
	if e != nil {
		return e
	}
	_, e = file.Write(raw)
	if e == nil {
		e = file.Sync()
	}
	closeErr := file.Close()
	if e == nil {
		e = closeErr
	}
	if e != nil {
		s.remove(name)
		return e
	}
	if e = s.root.Rename(name, ".recording-"+j.Recording.ID+".json"); e != nil {
		return e
	}
	return s.syncDir(".")
}

type contextReader struct {
	ctx    context.Context
	reader io.Reader
}

func (r contextReader) Read(v []byte) (int, error) {
	if e := r.ctx.Err(); e != nil {
		return 0, e
	}
	return r.reader.Read(v)
}
func (s *Storage) stat(ctx context.Context, relationship, user, id string) (store.RecordingReceipt, error) {
	name, e := Path(relationship, user, id)
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	info, e := s.info(name)
	if errors.Is(e, os.ErrNotExist) {
		return store.RecordingReceipt{}, store.ErrRecordingMissing
	}
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	if info.Size() <= 0 || info.Size() > store.MaxRecordingBytes {
		return store.RecordingReceipt{}, ErrRecovery
	}
	file, e := s.root.Open(name)
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	defer file.Close()
	opened, e := file.Stat()
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	if !os.SameFile(info, opened) {
		return store.RecordingReceipt{}, ErrRecovery
	}
	digest := sha256.New()
	size, e := io.CopyBuffer(digest, io.LimitReader(contextReader{ctx, file}, store.MaxRecordingBytes+1), make([]byte, 64*1024))
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	if size != info.Size() {
		return store.RecordingReceipt{}, ErrRecovery
	}
	return store.RecordingReceipt{ID: id, Bytes: size, SHA256: hex.EncodeToString(digest.Sum(nil)), Metadata: Metadata(file, size)}, nil
}
func (s *Storage) Stat(ctx context.Context, relationship, user, id string) (store.RecordingReceipt, error) {
	release, e := s.acquire(ctx, false)
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	defer release()
	if e = s.ready(); e != nil {
		return store.RecordingReceipt{}, e
	}
	return s.stat(ctx, relationship, user, id)
}

// The caller owns the per-recording lease throughout Upload. No volume lock is
// held while reading a potentially slow browser/node request body.
func (s *Storage) Upload(ctx context.Context, relationship string, v store.Recording, reader io.Reader, a store.NodeAudit) (store.RecordingReceipt, error) {
	if v.Bytes < 1 || v.Bytes > store.MaxRecordingBytes || !store.ValidRecordingFilename(v.Filename) || !store.RecordingContentType(v.ContentType) {
		return store.RecordingReceipt{}, store.ErrRecordingState
	}
	name, e := Path(relationship, v.UserID, v.ID)
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	if e = s.Ready(ctx); e != nil {
		return store.RecordingReceipt{}, e
	}
	n, e := s.nodes.ReadIdentity(ctx)
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	free, e := s.Free()
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	if n.Role == "Follower" {
		e = s.repo.ReserveOwnedRecording(ctx, relationship, v, free, a)
	} else if n.Role == "Master" && relationship == n.ID {
		e = s.repo.CheckLocalRecordingUpload(ctx, v)
	} else {
		e = store.ErrRecordingState
	}
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	if receipt, e := s.Stat(ctx, relationship, v.UserID, v.ID); e == nil {
		if receipt.Bytes != v.Bytes {
			return store.RecordingReceipt{}, store.ErrRecordingState
		}
		if n.Role == "Follower" {
			e = s.repo.CompleteOwnedRecording(ctx, relationship, receipt, free, a)
		}
		return receipt, e
	} else if !errors.Is(e, store.ErrRecordingMissing) {
		return store.RecordingReceipt{}, e
	}
	stage := ".recording-" + v.ID + ".part"
	release, e := s.acquire(ctx, false)
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	if e = s.ready(); e != nil {
		release()
		return store.RecordingReceipt{}, e
	}
	file, e := s.privateFile(stage, os.O_CREATE|os.O_EXCL|os.O_WRONLY)
	release()
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	digest := sha256.New()
	bytes, e := io.CopyBuffer(io.MultiWriter(file, digest), io.LimitReader(contextReader{ctx, reader}, v.Bytes+1), make([]byte, 64*1024))
	if e == nil && bytes != v.Bytes {
		e = ErrSize
	}
	if e == nil {
		e = ctx.Err()
	}
	if e == nil {
		e = file.Sync()
	}
	closeErr := file.Close()
	if e == nil {
		e = closeErr
	}
	if e != nil {
		if cleanup := s.remove(stage); cleanup != nil {
			s.fence()
		}
		return store.RecordingReceipt{}, e
	}
	receipt := store.RecordingReceipt{ID: v.ID, Bytes: bytes, SHA256: hex.EncodeToString(digest.Sum(nil))}
	release, e = s.acquire(ctx, true)
	if e != nil {
		s.remove(stage)
		return store.RecordingReceipt{}, e
	}
	defer release()
	if e = s.ready(); e != nil {
		s.remove(stage)
		return store.RecordingReceipt{}, e
	}
	j := journal{Version: 1, Operation: "upload", Relationship: relationship, Recording: v, UserID: v.UserID, MemberID: v.MemberID, Receipt: receipt, NodeAudit: a}
	if n.Role == "Follower" {
		j.MemberID = n.ID
	}
	if e = s.writeJournal(j); e != nil {
		s.fence()
		return receipt, e
	}
	if e = s.finishUpload(ctx, j); e != nil {
		s.fence()
		return receipt, e
	}
	receipt.Metadata = nil
	// Recoverable publication is complete; footer extraction needs no extra copy.
	file, e = s.root.Open(name)
	if e == nil {
		receipt.Metadata = Metadata(file, bytes)
		file.Close()
	}
	return receipt, e
}
func (s *Storage) clearIntent(id string) error {
	if e := s.remove(".recording-" + id + ".json"); e != nil {
		return e
	}
	return s.syncDir(".")
}
func (s *Storage) finishUpload(ctx context.Context, j journal) error {
	v := j.Recording
	v.UserID, v.MemberID = j.UserID, j.MemberID
	target, e := Path(j.Relationship, v.UserID, v.ID)
	if e != nil {
		return e
	}
	stage := ".recording-" + v.ID + ".part"
	current, e := s.repo.Recording(ctx, v.ID)
	if e != nil {
		return e
	}
	if current == nil || current.State == "deleting" || current.State == "deleted" {
		// A delete intent wins over a terminated upload. Only unlink a target
		// whose inode is demonstrably owned by this publication journal.
		staged, se := s.info(stage)
		actual, te := s.info(target)
		if te == nil {
			if se != nil || !os.SameFile(staged, actual) {
				return ErrRecovery
			}
			if e = s.remove(target); e != nil {
				return e
			}
			if e = s.syncDir(path.Dir(target)); e != nil {
				return e
			}
		} else if !errors.Is(te, os.ErrNotExist) {
			return te
		}
		if e = s.clearIntent(v.ID); e != nil {
			return e
		}
		return s.remove(stage)
	}
	if current.UserID != v.UserID || current.MemberID != v.MemberID || current.Bytes != j.Receipt.Bytes {
		return ErrRecovery
	}
	staged, se := s.info(stage)
	actual, te := s.info(target)
	if se != nil && !errors.Is(se, os.ErrNotExist) {
		return se
	}
	if te != nil && !errors.Is(te, os.ErrNotExist) {
		return te
	}
	if se != nil && te != nil {
		return ErrRecovery
	}
	if se == nil && staged.Size() != j.Receipt.Bytes || te == nil && (actual.Size() != j.Receipt.Bytes || se == nil && !os.SameFile(staged, actual)) {
		return ErrRecovery
	}
	if te != nil {
		if e = s.makeParents(target); e != nil {
			return e
		}
		if e = s.root.Link(stage, target); e != nil {
			return e
		}
		if e = s.syncDir(path.Dir(target)); e != nil {
			return e
		}
	}
	receipt, e := s.stat(ctx, j.Relationship, v.UserID, v.ID)
	if e != nil {
		return e
	}
	if receipt.SHA256 != j.Receipt.SHA256 || receipt.Bytes != j.Receipt.Bytes {
		return ErrRecovery
	}
	n, e := s.nodes.ReadIdentity(ctx)
	if e != nil {
		return e
	}
	free, e := s.Free()
	if e != nil {
		return e
	}
	if n.Role == "Follower" {
		e = s.repo.CompleteOwnedRecording(ctx, j.Relationship, receipt, free, j.NodeAudit)
	} else if n.Role == "Master" {
		e = s.repo.CheckLocalRecordingUpload(ctx, v)
	} else {
		e = store.ErrRecordingState
	}
	if e != nil {
		return e
	}
	if e = s.clearIntent(v.ID); e != nil {
		return e
	}
	if e = s.remove(stage); e != nil {
		return e
	}
	return s.syncDir(".")
}

// Open holds a shared volume lease until the response body is closed.
func (s *Storage) Open(ctx context.Context, relationship, user, id string) (*os.File, os.FileInfo, func(), error) {
	release, e := s.acquire(ctx, false)
	if e != nil {
		return nil, nil, nil, e
	}
	fail := func(e error) (*os.File, os.FileInfo, func(), error) { release(); return nil, nil, nil, e }
	if e = s.ready(); e != nil {
		return fail(e)
	}
	name, e := Path(relationship, user, id)
	if e != nil {
		return fail(e)
	}
	row, e := s.repo.Recording(ctx, id)
	if e != nil {
		return fail(e)
	}
	if row != nil && (row.UserID != user || row.State != "ready") {
		return fail(store.ErrRecordingMissing)
	}
	info, e := s.info(name)
	if errors.Is(e, os.ErrNotExist) {
		return fail(store.ErrRecordingMissing)
	}
	if e != nil {
		return fail(e)
	}
	file, e := s.root.Open(name)
	if e != nil {
		return fail(e)
	}
	actual, e := file.Stat()
	if e != nil || !os.SameFile(info, actual) {
		file.Close()
		return fail(ErrRecovery)
	}
	return file, info, func() { file.Close(); release() }, nil
}

// The caller owns the recording lease. Unknown filesystem/SQL outcomes retain
// intent and accounting; replay reconciles DB truth before touching quarantine.
func (s *Storage) Delete(ctx context.Context, relationship, user, id string, a store.KaraokeAudit, na store.NodeAudit) error {
	name, e := Path(relationship, user, id)
	if e != nil {
		return e
	}
	release, e := s.acquire(ctx, true)
	if e != nil {
		return e
	}
	defer release()
	if e = s.ready(); e != nil {
		return e
	}
	n, e := s.nodes.ReadIdentity(ctx)
	if e != nil {
		return e
	}
	var v *store.Recording
	if n.Role == "Follower" {
		v, e = s.repo.StageOwnedRecordingDeletion(ctx, relationship, user, id)
	} else if n.Role == "Master" && relationship == n.ID {
		v, e = s.repo.Recording(ctx, id)
		if v != nil && (v.UserID != user || v.MemberID != n.ID || v.State != "deleting") {
			e = store.ErrRecordingState
		}
	} else {
		e = store.ErrRecordingState
	}
	if e != nil {
		return e
	}
	info, physical := s.info(name)
	if physical != nil && !errors.Is(physical, os.ErrNotExist) {
		return physical
	}
	if v == nil || v.State == "deleted" {
		if physical == nil {
			return ErrRecovery
		}
		return nil
	}
	if physical == nil && info.Size() > v.Bytes {
		return store.ErrRecordingState
	}
	j := journal{Version: 1, Operation: "delete", Relationship: relationship, Recording: *v, UserID: user, MemberID: v.MemberID, Audit: a, NodeAudit: na, Absent: errors.Is(physical, os.ErrNotExist)}
	if e = s.writeJournal(j); e != nil {
		s.fence()
		return e
	}
	if e = s.finishDelete(ctx, j); e != nil {
		s.fence()
		return e
	}
	return nil
}
func (s *Storage) finishDelete(ctx context.Context, j journal) error {
	id := j.Recording.ID
	name, e := Path(j.Relationship, j.UserID, id)
	if e != nil {
		return e
	}
	trash := ".recording-" + id + ".deleted"
	current, e := s.repo.Recording(ctx, id)
	if e != nil {
		return e
	}
	committed := current == nil || current.State == "deleted"
	if !committed {
		if current.UserID != j.UserID || current.MemberID != j.MemberID || current.State != "deleting" || current.Bytes != j.Recording.Bytes {
			return ErrRecovery
		}
		_, source := s.info(name)
		_, quarantine := s.info(trash)
		if j.Absent {
			if source == nil || quarantine == nil {
				return ErrRecovery
			}
			if !errors.Is(source, os.ErrNotExist) {
				return source
			}
			if !errors.Is(quarantine, os.ErrNotExist) {
				return quarantine
			}
		} else {
			if source == nil && errors.Is(quarantine, os.ErrNotExist) {
				if e = s.root.Rename(name, trash); e != nil {
					return e
				}
				if e = s.syncDir(path.Dir(name)); e != nil {
					return e
				}
				if e = s.syncDir("."); e != nil {
					return e
				}
			} else if !errors.Is(source, os.ErrNotExist) || quarantine != nil {
				return ErrRecovery
			}
		}
		n, e := s.nodes.ReadIdentity(ctx)
		if e != nil {
			return e
		}
		free, e := s.Free()
		if e != nil {
			return e
		}
		if n.Role == "Follower" {
			e = s.repo.CompleteOwnedRecordingDeletion(ctx, j.Relationship, j.UserID, id, free, j.NodeAudit)
		} else if n.Role == "Master" {
			e = s.repo.CompleteRecordingDeletion(ctx, j.UserID, id, j.Audit)
		} else {
			e = store.ErrRecordingState
		}
		if e != nil {
			confirmed, readErr := s.repo.Recording(ctx, id)
			if readErr != nil {
				return errors.Join(e, readErr)
			}
			if confirmed != nil && confirmed.State != "deleted" {
				if !j.Absent {
					if _, targetErr := s.info(name); !errors.Is(targetErr, os.ErrNotExist) {
						return errors.Join(e, ErrRecovery)
					}
					if restore := s.root.Rename(trash, name); restore != nil {
						return errors.Join(e, restore)
					}
					if syncErr := s.syncDir(path.Dir(name)); syncErr != nil {
						return errors.Join(e, syncErr)
					}
					if syncErr := s.syncDir("."); syncErr != nil {
						return errors.Join(e, syncErr)
					}
				}
				if clear := s.clearIntent(id); clear != nil {
					return errors.Join(e, clear)
				}
				return e
			}
		}
	}
	// Never restore a committed deletion. Its quota was already refunded.
	if e = s.remove(trash); e != nil {
		return e
	}
	if e = s.syncDir("."); e != nil {
		return e
	}
	return s.clearIntent(id)
}
func (s *Storage) Recover(ctx context.Context) error {
	release, e := s.acquire(ctx, true)
	if e != nil {
		return e
	}
	defer release()
	directory, e := s.root.Open(".")
	if e != nil {
		return e
	}
	entries, e := directory.ReadDir(-1)
	directory.Close()
	if e != nil {
		return e
	}
	deferredIntent := false
	for _, entry := range entries {
		match := journalName.FindStringSubmatch(entry.Name())
		if match == nil {
			continue
		}
		unlock, e := s.Lock(match[1])
		if errors.Is(e, filelease.ErrBusy) {
			deferredIntent = true
			continue
		}
		if e != nil {
			return e
		}
		e = func() error {
			defer unlock()
			info, e := s.info(entry.Name())
			if e != nil {
				return e
			}
			if info.Size() > store.MaxRecordingMetadata+16384 {
				return ErrRecovery
			}
			file, e := s.root.Open(entry.Name())
			if e != nil {
				return e
			}
			decoder := json.NewDecoder(io.LimitReader(file, store.MaxRecordingMetadata+16385))
			var j journal
			e = decoder.Decode(&j)
			var extra any
			if e == nil && decoder.Decode(&extra) != io.EOF {
				e = ErrRecovery
			}
			file.Close()
			if e != nil {
				return e
			}
			if j.Version != 1 || j.Recording.ID != match[1] || !identifier.MatchString(j.UserID) || !identifier.MatchString(j.MemberID) || !identifier.MatchString(j.Relationship) {
				return ErrRecovery
			}
			if j.Operation == "upload" {
				return s.finishUpload(ctx, j)
			}
			if j.Operation == "delete" {
				return s.finishDelete(ctx, j)
			}
			return ErrRecovery
		}()
		if e != nil {
			s.fence()
			return e
		}
	}
	for _, entry := range entries {
		match := stageName.FindStringSubmatch(entry.Name())
		if match == nil {
			match = temporaryName.FindStringSubmatch(entry.Name())
		}
		if match == nil {
			continue
		}
		unlock, e := s.Lock(match[1])
		if errors.Is(e, filelease.ErrBusy) {
			continue
		}
		if e != nil {
			return e
		}
		_, intent := s.root.Lstat(".recording-" + match[1] + ".json")
		if errors.Is(intent, os.ErrNotExist) {
			e = s.remove(entry.Name())
		}
		unlock()
		if e != nil {
			return e
		}
	}
	if deferredIntent {
		s.fence()
		return ErrRecovery
	}
	if e = s.remove(".recordings-recovery-required"); e != nil {
		return e
	}
	return s.syncDir(".")
}
