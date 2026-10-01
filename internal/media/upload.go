package media

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path"
	"regexp"
	"sort"
	"strings"
	"sync"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

var ErrUploadSize = errors.New("上传失败，文件过大")
var ErrSignature = errors.New("上传失败，文件内容不是受支持的媒体格式")
var ErrLayout = errors.New("该分类不能同时使用直接媒体与子目录媒体")
var uploadStageName = regexp.MustCompile(`^\.upload-([0-9a-f]{32})\.part$`)
var uploadJournalName = regexp.MustCompile(`^\.upload-([0-9a-f]{32})\.json$`)
var uploadJournalTemporaryName = regexp.MustCompile(`^\.upload-[0-9a-f]{32}\.journal\.tmp$`)
var uploadLeaseName = regexp.MustCompile(`^\.upload-[0-9a-f]{32}\.lease$`)

const MaxLyricUploadBytes int64 = 2 * 1024 * 1024

type Stage struct {
	service  *Service
	name, id string
	Bytes    int64
	Digest   string
	Head     []byte
	retain   bool
	close    sync.Once
	lease    func()
}

func (s *Stage) Close() {
	s.close.Do(func() {
		if s.lease != nil {
			s.lease()
			s.service.root.Remove(".upload-" + s.id + ".lease")
		}
		if !s.retain {
			if err := s.service.root.Remove(s.name); err != nil && !errors.Is(err, os.ErrNotExist) {
				slog.Error("upload stage cleanup deferred", "operation", s.id, "error", err)
			}
		}
	})
}

type headWriter struct{ value []byte }

func (h *headWriter) Write(value []byte) (int, error) {
	n := len(value)
	if len(h.value) < 4096 {
		h.value = append(h.value, value[:min(len(value), 4096-len(h.value))]...)
	}
	return n, nil
}

func (s *Service) Stage(ctx context.Context, reader io.Reader, maximum int64) (*Stage, error) {
	return s.stage(ctx, reader, maximum, nil)
}

func (s *Service) stage(ctx context.Context, reader io.Reader, maximum int64, prepare func(*Stage) error) (*Stage, error) {
	if err := s.Ready(ctx); err != nil {
		return nil, err
	}
	if maximum < 1 || maximum == int64(^uint64(0)>>1) {
		return nil, ErrUploadSize
	}
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		return nil, err
	}
	id := hex.EncodeToString(random)
	stage := &Stage{service: s, id: id, name: ".upload-" + id + ".part"}
	// Startup recovery may overlap another worker's active multipart request.
	// Establish a process lease before making the stage visible, while holding
	// the volume's shared lease. No volume lease is held during network reads.
	volumeRelease, err := s.acquire(ctx, false)
	if err != nil {
		return nil, err
	}
	if err := s.ready(); err != nil {
		volumeRelease()
		return nil, err
	}
	leaseFile, err := s.root.OpenFile(".upload-"+id+".lease", os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		volumeRelease()
		return nil, err
	}
	stage.lease, err = filelease.Acquire(ctx, leaseFile, true)
	if err != nil {
		volumeRelease()
		stage.Close()
		return nil, err
	}
	f, err := s.root.OpenFile(stage.name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	volumeRelease()
	if err != nil {
		stage.Close()
		return nil, err
	}
	if prepare != nil {
		if err := prepare(stage); err != nil {
			f.Close()
			stage.Close()
			return nil, err
		}
	}
	digest := sha256.New()
	head := &headWriter{}
	buffer := make([]byte, 64*1024)
	total, err := io.CopyBuffer(io.MultiWriter(f, digest, head), io.LimitReader(&contextReader{ctx, reader}, maximum+1), buffer)
	if err == nil && total > maximum {
		err = ErrUploadSize
	}
	if err == nil {
		err = ctx.Err()
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		stage.Close()
		return nil, err
	}
	stage.Bytes = total
	stage.Digest = hex.EncodeToString(digest.Sum(nil))
	stage.Head = head.value
	return stage, nil
}

func signature(extension string, head []byte) bool {
	switch extension {
	case ".mp3":
		return bytes.HasPrefix(head, []byte("ID3")) || len(head) >= 2 && head[0] == 0xff && head[1]&0xe0 == 0xe0
	case ".flac":
		return bytes.HasPrefix(head, []byte("fLaC"))
	case ".wav":
		return len(head) >= 12 && string(head[:4]) == "RIFF" && string(head[8:12]) == "WAVE"
	case ".m4a", ".mp4":
		return len(head) >= 12 && string(head[4:8]) == "ftyp"
	case ".webm", ".mkv":
		return bytes.HasPrefix(head, []byte{0x1a, 0x45, 0xdf, 0xa3})
	}
	return false
}

func relativeUpload(value string) (string, error) {
	value = strings.TrimSpace(strings.ReplaceAll(value, "\\", "/"))
	if value == "" || strings.HasPrefix(value, "/") || strings.ContainsAny(value, "\x00:") {
		return "", ErrPath
	}
	parts := []string{}
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." {
			continue
		}
		if strings.HasPrefix(part, ".") || !utf8.ValidString(part) || len(part) > 255 {
			return "", ErrPath
		}
		parts = append(parts, part)
	}
	if len(parts) == 0 || len(parts) > 4 {
		return "", ErrPath
	}
	return strings.Join(parts, "/"), nil
}

func (s *Service) destination(filename, target, relative string, lyric bool, maxName int) (store.MediaObject, error) {
	return s.uploadDestination(filename, target, relative, lyric, maxName, false)
}

func (s *Service) uploadDestination(filename, target, relative string, lyric bool, maxName int, virtual bool) (store.MediaObject, error) {
	filename = path.Base(strings.ReplaceAll(filename, "\\", "/"))
	if relative != "" {
		normalized, err := relativeUpload(relative)
		if err != nil {
			return store.MediaObject{}, err
		}
		relative = normalized
		filename = path.Base(normalized)
	}
	filename, err := s.search.Simplify(strings.TrimSpace(filename))
	if err != nil {
		return store.MediaObject{}, err
	}
	if filename == "" || !utf8.ValidString(filename) || utf8.RuneCountInString(filename) > maxName || len(filename) > 255 || strings.HasPrefix(filename, ".") || strings.ContainsAny(filename, "/\\\x00:") {
		return store.MediaObject{}, ErrPath
	}
	name := ""
	kind := "audio"
	if lyric {
		name = "lyrics/" + filename
		if relative != "" {
			parent := path.Dir(relative)
			if parent != "." {
				name = "lyrics/" + parent + "/" + filename
			}
		}
		kind = "lyric"
	} else {
		base := ""
		if target != "" {
			base, err = normalizeAdminPath(target, false)
			if err != nil {
				return store.MediaObject{}, err
			}
			if !virtual {
				info, err := s.safeInfo(base)
				if err != nil {
					return store.MediaObject{}, err
				}
				if !info.IsDir() {
					return store.MediaObject{}, ErrPath
				}
			}
		}
		if relative != "" {
			if parent := path.Dir(relative); parent != "." {
				if base != "" {
					base += "/"
				}
				base += parent
			}
		} else if base == "" {
			return store.MediaObject{}, ErrPath
		}
		if base != "" {
			name = base + "/"
		}
		name += filename
		if strings.HasPrefix(name, "vido/") {
			kind = "video"
		}
	}
	if !managedObject(name, false) || lyric && !strings.HasPrefix(name, "lyrics/") || !lyric && strings.HasPrefix(name, "lyrics/") {
		return store.MediaObject{}, ErrPath
	}
	return store.MediaObject{Path: name, Kind: kind}, nil
}

func (s *Service) checkLayout(object store.MediaObject) error {
	if object.Kind == "lyric" {
		return nil
	}
	parts := strings.Split(object.Path, "/")
	category := strings.Join(parts[:2], "/")
	entries, err := s.readDir(category)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if len(parts) == 4 && entry.Type().IsRegular() && validExt(parts[0], entry.Name()) {
			return ErrLayout
		}
		if len(parts) == 3 && entry.IsDir() {
			files, err := s.readDir(category + "/" + entry.Name())
			if err != nil {
				return err
			}
			for _, file := range files {
				if file.Type().IsRegular() && validExt(parts[0], file.Name()) {
					return ErrLayout
				}
			}
		}
	}
	return nil
}

func (s *Service) makeParents(name string) error {
	parts := strings.Split(path.Dir(name), "/")
	for i := range parts {
		directory := strings.Join(parts[:i+1], "/")
		info, err := s.root.Lstat(directory)
		if err == nil {
			if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
				return ErrPath
			}
			continue
		}
		if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if err := s.root.Mkdir(directory, 0755); err != nil {
			return err
		}
		if err := s.syncDirectory(path.Dir(directory)); err != nil {
			return err
		}
	}
	return nil
}

type uploadJournal struct {
	Format          string            `json:"format"`
	Version         int               `json:"version"`
	ID              string            `json:"id"`
	Object          store.MediaObject `json:"object"`
	Bytes           int64             `json:"bytes"`
	SHA256          string            `json:"sha256"`
	Audit           store.AdminAudit  `json:"audit"`
	NodeAudit       store.NodeAudit   `json:"node_audit,omitempty"`
	UploadSessionID string            `json:"upload_session_id,omitempty"`
}

func journalPath(id string) string { return ".upload-" + id + ".json" }
func limited(value string, maximum int) string {
	runes := []rune(value)
	if len(runes) > maximum {
		runes = runes[:maximum]
	}
	return string(runes)
}

func (s *Service) writeUploadJournal(journal uploadJournal) error {
	data, err := json.Marshal(journal)
	if err != nil {
		return err
	}
	temporary := ".upload-" + journal.ID + ".journal.tmp"
	f, err := s.root.OpenFile(temporary, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer s.root.Remove(temporary)
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
	if err := s.root.Rename(temporary, journalPath(journal.ID)); err != nil {
		return err
	}
	return s.syncDirectory(".")
}

func (s *Service) finishUpload(ctx context.Context, journal uploadJournal) error {
	owned := journal.Format == "frontiercloud-owned-upload"
	master := journal.Format == "frontiercloud-master-upload"
	if (!owned && !master && journal.Format != "frontiercloud-local-upload") || journal.Version != 1 || !operationID.MatchString(journal.ID) || !managedObject(journal.Object.Path, false) || journal.Bytes < 1 || len(journal.SHA256) != 64 {
		return ErrRecovery
	}
	if _, err := hex.DecodeString(journal.SHA256); err != nil {
		return ErrRecovery
	}
	parts := strings.Split(journal.Object.Path, "/")
	expected := "audio"
	action := "upload_item"
	if parts[0] == "vido" {
		expected = "video"
	}
	if parts[0] == "lyrics" {
		expected = "lyric"
		action = "upload_lyric"
	}
	if journal.Object.Kind != expected || !owned && !master && journal.Audit.Action != action || owned && (s.owned == nil || expected == "lyric" || !ownedObjectID.MatchString(journal.Object.ID)) || master && (s.pool == nil || expected == "lyric" || !ownedObjectID.MatchString(journal.Object.ID) || !operationID.MatchString(journal.UploadSessionID)) {
		return ErrRecovery
	}
	stageName := ".upload-" + journal.ID + ".part"
	staged, stageErr := s.root.Lstat(stageName)
	if stageErr != nil && !errors.Is(stageErr, os.ErrNotExist) {
		return stageErr
	}
	if stageErr == nil && (!staged.Mode().IsRegular() || staged.Mode()&os.ModeSymlink != 0 || staged.Size() != journal.Bytes) {
		return ErrRecovery
	}
	target, targetErr := s.safeInfo(journal.Object.Path)
	if targetErr != nil && !errors.Is(targetErr, os.ErrNotExist) {
		return targetErr
	}
	if targetErr == nil && (!target.Mode().IsRegular() || target.Size() != journal.Bytes || stageErr == nil && !os.SameFile(staged, target)) {
		return os.ErrExist
	}
	if targetErr != nil && stageErr != nil {
		return ErrRecovery
	}
	if targetErr != nil {
		if err := s.checkLayout(journal.Object); err != nil {
			return err
		}
		if err := s.makeParents(journal.Object.Path); err != nil {
			return err
		}
		if err := s.root.Chmod(stageName, 0644); err != nil {
			return err
		}
		f, err := s.root.OpenFile(stageName, os.O_RDWR, 0)
		if err != nil {
			return err
		}
		err = f.Sync()
		f.Close()
		if err != nil {
			return err
		}
		if err := s.root.Link(stageName, journal.Object.Path); err != nil {
			return err
		}
		if err := s.syncDirectory(path.Dir(journal.Object.Path)); err != nil {
			return err
		}
	}
	var completion error
	if owned || master {
		_, free, err := fsutil.DiskUsage(s.root)
		if err != nil {
			return err
		}
		if master {
			completion = s.pool.CompleteMasterUpload(ctx, journal.UploadSessionID, journal.Object, journal.Bytes, `"`+journal.SHA256+`"`, free, journal.Audit)
		} else {
			completion = s.owned.CompleteOwnedUpload(ctx, journal.ID, journal.Object, journal.Bytes, `"`+journal.SHA256+`"`, free, journal.NodeAudit)
		}
	} else {
		completion = s.repository.CompleteUpload(ctx, journal.Object, journal.ID, journal.Audit)
	}
	if completion != nil {
		return completion
	}
	// Remove and sync the replay intent BEFORE unlinking its staged hard link.
	// A later media deletion must not be resurrected by an old upload journal.
	if err := s.root.Remove(journalPath(journal.ID)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := s.syncDirectory("."); err != nil {
		return err
	}
	if err := s.root.Remove(stageName); err != nil && !errors.Is(err, os.ErrNotExist) {
		slog.Error("completed upload stage cleanup deferred", "operation", journal.ID, "error", err)
	}
	return nil
}

func (s *Service) Publish(ctx context.Context, stage *Stage, filename, target, relative string, lyric bool, maxName int, audit store.AdminAudit) (string, error) {
	if stage == nil || stage.service != s || !uploadStageName.MatchString(stage.name) {
		return "", ErrPath
	}
	if stage.Bytes == 0 {
		return "", ErrSignature
	}
	if lyric {
		if stage.Bytes > MaxLyricUploadBytes {
			return "", ErrUploadSize
		}
		f, err := s.root.Open(stage.name)
		if err != nil {
			return "", err
		}
		payload, err := io.ReadAll(io.LimitReader(&contextReader{ctx, f}, MaxLyricUploadBytes+1))
		f.Close()
		if err != nil {
			return "", err
		}
		if int64(len(payload)) > MaxLyricUploadBytes {
			return "", ErrUploadSize
		}
		if _, err := ParseLRC(payload); err != nil {
			return "", fmt.Errorf("%w: %v", ErrSignature, err)
		}
	}
	release, leaseErr := s.acquire(ctx, true)
	if leaseErr != nil {
		return "", leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return "", err
	}
	if err := ctx.Err(); err != nil {
		return "", err
	}
	object, err := s.destination(filename, target, relative, lyric, maxName)
	if err != nil {
		return "", err
	}
	if !lyric && !signature(strings.ToLower(path.Ext(object.Path)), stage.Head) {
		return "", ErrSignature
	}
	if _, err := s.safeInfo(object.Path); err == nil {
		return "", os.ErrExist
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	if err := s.checkLayout(object); err != nil {
		return "", err
	}
	audit.Action = "upload_item"
	if lyric {
		audit.Action = "upload_lyric"
	}
	audit.SessionHash = limited(audit.SessionHash, 64)
	audit.SourceSummary = ""
	audit.Detail = ""
	audit.ClientIP = limited(audit.ClientIP, 45)
	audit.UserAgent = limited(audit.UserAgent, 512)
	audit.RequestID = limited(audit.RequestID, 128)
	audit.TraceID = limited(audit.TraceID, 64)
	journal := uploadJournal{Format: "frontiercloud-local-upload", Version: 1, ID: stage.id, Object: object, Bytes: stage.Bytes, SHA256: stage.Digest, Audit: audit}
	// Retain the staged bytes if durable intent or publication is uncertain.
	stage.retain = true
	if err := s.writeUploadJournal(journal); err != nil {
		s.markRecovery()
		return "", fmt.Errorf("%w: %v", ErrRecovery, err)
	}
	if err := s.finishUpload(ctx, journal); err != nil {
		s.markRecovery()
		return "", fmt.Errorf("%w: %v", ErrRecovery, err)
	}
	return object.Path, nil
}

func (s *Service) verifyUploadBytes(ctx context.Context, journal uploadJournal) error {
	name := ".upload-" + journal.ID + ".part"
	info, err := s.root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		name = journal.Object.Path
		info, err = s.safeInfo(name)
	}
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Size() != journal.Bytes {
		return ErrRecovery
	}
	f, err := s.root.Open(name)
	if err != nil {
		return err
	}
	defer f.Close()
	digest := sha256.New()
	count, err := io.CopyBuffer(digest, &contextReader{ctx, f}, make([]byte, 64*1024))
	if err != nil {
		return err
	}
	if count != journal.Bytes || hex.EncodeToString(digest.Sum(nil)) != journal.SHA256 {
		return ErrRecovery
	}
	return nil
}

func (s *Service) recoverUploads(ctx context.Context) error {
	f, err := s.root.Open(".")
	if err != nil {
		return err
	}
	entries, err := f.ReadDir(-1)
	f.Close()
	if err != nil {
		return err
	}
	journals := []string{}
	for _, entry := range entries {
		if uploadJournalName.MatchString(entry.Name()) {
			if !entry.Type().IsRegular() {
				return ErrRecovery
			}
			journals = append(journals, entry.Name())
		}
	}
	sort.Strings(journals)
	for _, name := range journals {
		if err := ctx.Err(); err != nil {
			return err
		}
		f, err := s.root.Open(name)
		if err != nil {
			return err
		}
		payload, err := io.ReadAll(io.LimitReader(f, 65537))
		f.Close()
		if err != nil {
			return err
		}
		if len(payload) > 65536 {
			return ErrRecovery
		}
		var journal uploadJournal
		decoder := json.NewDecoder(bytes.NewReader(payload))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&journal); err != nil {
			return fmt.Errorf("invalid upload journal %s: %w", name, err)
		}
		var extra any
		if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
			return ErrRecovery
		}
		if journal.ID != uploadJournalName.FindStringSubmatch(name)[1] || (journal.Format != "frontiercloud-local-upload" && journal.Format != "frontiercloud-owned-upload" && journal.Format != "frontiercloud-master-upload") || journal.Version != 1 || !managedObject(journal.Object.Path, false) {
			return ErrRecovery
		}
		if err := s.verifyUploadBytes(ctx, journal); err != nil {
			return fmt.Errorf("verify upload %s: %w", journal.ID, err)
		}
		if err := s.finishUpload(ctx, journal); err != nil {
			return fmt.Errorf("recover upload %s: %w", journal.ID, err)
		}
	}
	// Another worker may be uploading. Remove only stages with no live process
	// lease; an OS crash releases that lease without depending on a TTL guess.
	for _, entry := range entries {
		name := entry.Name()
		if uploadStageName.MatchString(name) || uploadJournalTemporaryName.MatchString(name) || uploadLeaseName.MatchString(name) {
			id := strings.TrimPrefix(strings.Split(name, ".")[1], "upload-")
			leaseName := ".upload-" + id + ".lease"
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
				if err := s.root.Remove(leaseName); err != nil && !errors.Is(err, os.ErrNotExist) {
					return err
				}
			} else if !errors.Is(err, os.ErrNotExist) {
				return err
			}
			if err := s.root.Remove(name); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
	}
	return s.syncDirectory(".")
}
