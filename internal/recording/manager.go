package recording

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

var ErrUnavailable = errors.New("录音存储节点暂不可用")
var digestPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

type Manager struct {
	repo     store.RecordingRepository
	accounts store.KaraokeRepository
	nodes    store.NodeRepository
	pool     store.PoolRepository
	control  *node.Service
	volume   *Storage
}

func NewManager(repo store.RecordingRepository, accounts store.KaraokeRepository, nodes store.NodeRepository, pool store.PoolRepository, control *node.Service, volume *Storage) *Manager {
	return &Manager{repo, accounts, nodes, pool, control, volume}
}

type Ticket struct {
	ID         string  `json:"recording_id"`
	Filename   string  `json:"filename"`
	URL        string  `json:"upload_url"`
	Direct     bool    `json:"direct"`
	Capability *string `json:"capability"`
}
type Placement struct {
	Recording    store.Recording
	Member       store.StorageMember
	Relationship *store.Relationship
	Local        bool
}

func (s *Manager) master(ctx context.Context) (store.NodeIdentity, error) {
	n, e := s.nodes.ReadIdentity(ctx)
	if e == nil && n.Role != "Master" {
		e = store.ErrNodeState
	}
	return n, e
}
func (s *Manager) placement(ctx context.Context, user, id string, online bool) (Placement, error) {
	n, e := s.master(ctx)
	if e != nil {
		return Placement{}, e
	}
	v, e := s.repo.Recording(ctx, id)
	if e != nil {
		return Placement{}, e
	}
	if v == nil || v.UserID != user || v.State == "deleted" {
		return Placement{}, store.ErrRecordingMissing
	}
	members, e := s.pool.Members(ctx)
	if e != nil {
		return Placement{}, e
	}
	for _, m := range members {
		if m.ID != v.MemberID {
			continue
		}
		p := Placement{Recording: *v, Member: m, Local: m.ID == n.ID && m.Kind == "MasterLocal"}
		if !p.Local {
			rel, e := s.control.RecordingRelation(ctx, *v, m)
			if e != nil {
				return Placement{}, e
			}
			p.Relationship = &rel
		}
		if online && (m.Health != "online" || !p.Local && (p.Relationship.Status != "online" || time.Now().Unix()-p.Relationship.LastHeartbeat >= 120)) {
			return Placement{}, ErrUnavailable
		}
		return p, nil
	}
	return Placement{}, ErrUnavailable
}

var forbiddenName = regexp.MustCompile(`[\\/:*?"<>|\x00-\x1f]+`)

func Filename(title, username, id, ct string, now time.Time) string {
	title = strings.Trim(forbiddenName.ReplaceAllString(title, "_"), " ._")
	if title == "" {
		title = "卡拉OK录音"
	}
	runes := []rune(title)
	title = string(runes[:min(80, len(runes))])
	var name strings.Builder
	for _, r := range username {
		if unicode.IsLetter(r) || unicode.IsNumber(r) || r == '_' || r == '-' {
			name.WriteRune(r)
		} else {
			name.WriteByte('_')
		}
	}
	runes = []rune(name.String())
	username = string(runes[:min(32, len(runes))])
	if username == "" {
		username = "user"
	}
	extension := map[string]string{"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav"}[ct]
	if extension == "" {
		extension = ".bin"
	}
	return now.Format("20060102-150405") + "_" + title + "_" + username + "_" + id[:8] + extension
}
func (s *Manager) Ticket(ctx context.Context, user store.KaraokeUser, size int64, ct string, metadata store.RecordingMetadata, a store.KaraokeAudit) (Ticket, error) {
	if _, e := s.master(ctx); e != nil {
		return Ticket{}, e
	}
	if !store.ValidRecordingMetadata(metadata) {
		return Ticket{}, store.ErrRecordingState
	}
	bytes := make([]byte, 16)
	if _, e := rand.Read(bytes); e != nil {
		return Ticket{}, e
	}
	id := hex.EncodeToString(bytes)
	free, e := s.volume.Free()
	if e != nil {
		return Ticket{}, e
	}
	v, m, e := s.repo.ReserveRecording(ctx, store.Recording{ID: id, UserID: user.ID, Bytes: size, ContentType: ct, Filename: Filename(metadata.Title, user.Username, id, ct, time.Now()), Title: metadata.Title, Lyrics: metadata.Lyrics}, free, a)
	if e != nil {
		return Ticket{}, e
	}
	ticket := Ticket{ID: id, Filename: v.Filename, URL: "/api/v1/karaoke/account/recordings/" + id + "/content"}
	if m.RelationshipID != nil && m.Transport == "Direct" {
		rel, e := s.control.RecordingRelation(ctx, v, m)
		if e != nil {
			return Ticket{}, e
		}
		token, e := s.control.RecordingCapability(ctx, v, m, "upload")
		if e != nil {
			return Ticket{}, e
		}
		ticket.Direct = true
		ticket.Capability = &token
		ticket.URL = rel.Endpoint + "/internal/v1/recordings/" + id
	}
	return ticket, nil
}
func Receipt(value map[string]any, id string, maximum int64) (store.RecordingReceipt, error) {
	if metadata, present := value["metadata"]; present && metadata != nil {
		m, ok := metadata.(map[string]any)
		if !ok {
			return store.RecordingReceipt{}, store.ErrRecordingState
		}
		if lyrics, present := m["lyrics"]; present {
			lines, ok := lyrics.([]any)
			if !ok || len(lines) > 10000 {
				return store.RecordingReceipt{}, store.ErrRecordingState
			}
			for _, line := range lines {
				fields, ok := line.(map[string]any)
				if !ok {
					return store.RecordingReceipt{}, store.ErrRecordingState
				}
				switch fields["time"].(type) {
				case json.Number, float64:
				default:
					return store.RecordingReceipt{}, store.ErrRecordingState
				}
				if _, ok := fields["text"].(string); !ok {
					return store.RecordingReceipt{}, store.ErrRecordingState
				}
			}
		}
	}
	raw, e := json.Marshal(value)
	if e != nil || len(raw) > 5*1024*1024 {
		return store.RecordingReceipt{}, store.ErrRecordingState
	}
	var receipt store.RecordingReceipt
	if e = json.Unmarshal(raw, &receipt); e != nil {
		return receipt, store.ErrRecordingState
	}
	if receipt.ID == "" {
		receipt.ID = id
	}
	if receipt.ID != id || receipt.Bytes <= 0 || receipt.Bytes > maximum || !digestPattern.MatchString(receipt.SHA256) || receipt.Metadata != nil && !store.ValidRecordingMetadata(*receipt.Metadata) {
		return receipt, store.ErrRecordingState
	}
	return receipt, nil
}
func (s *Manager) Upload(ctx context.Context, user, id string, reader io.Reader, a store.KaraokeAudit) (store.RecordingReceipt, error) {
	unlock, e := s.volume.Lock(id)
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	defer unlock()
	p, e := s.placement(ctx, user, id, true)
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	if p.Recording.State != "pending" || !p.Local && p.Relationship.Mode != "Relay" {
		return store.RecordingReceipt{}, store.ErrRecordingState
	}
	if p.Local {
		return s.volume.Upload(ctx, p.Member.ID, p.Recording, reader, store.NodeAudit{RequestID: a.RequestID, TraceID: a.TraceID})
	}
	value, e := s.control.UploadRecording(ctx, p.Recording, p.Member, reader)
	if e != nil {
		return store.RecordingReceipt{}, e
	}
	return Receipt(value, id, p.Recording.Bytes)
}
func (s *Manager) PendingUpload(ctx context.Context, user, id string) (store.Recording, error) {
	p, e := s.placement(ctx, user, id, true)
	if e == nil && (p.Recording.State != "pending" || !p.Local && p.Relationship.Mode != "Relay") {
		e = store.ErrRecordingState
	}
	return p.Recording, e
}
func (s *Manager) Finalize(ctx context.Context, user, id string, a store.KaraokeAudit) error {
	unlock, e := s.volume.Lock(id)
	if e != nil {
		return e
	}
	defer unlock()
	p, e := s.placement(ctx, user, id, false)
	if e != nil {
		return e
	}
	if p.Recording.State == "ready" {
		return nil
	}
	if p.Recording.State != "pending" {
		return store.ErrRecordingState
	}
	var receipt store.RecordingReceipt
	if p.Local {
		receipt, e = s.volume.Stat(ctx, p.Member.ID, user, id)
	} else {
		var value map[string]any
		value, e = s.control.StatRecording(ctx, p.Recording, p.Member)
		if e == nil {
			receipt, e = Receipt(value, id, p.Recording.Bytes)
		}
	}
	if e != nil {
		return e
	}
	return s.repo.FinalizeRecording(ctx, user, id, receipt, a)
}
func (s *Manager) List(ctx context.Context, user string) ([]store.Recording, error) {
	if _, e := s.master(ctx); e != nil {
		return nil, e
	}
	return s.repo.ListRecordings(ctx, user, "ready", 500)
}
func (s *Manager) Delivery(ctx context.Context, user, id string, download, nginx bool) (Placement, string, error) {
	p, e := s.placement(ctx, user, id, true)
	if e != nil {
		return p, "", e
	}
	if p.Recording.State != "ready" {
		return p, "", store.ErrRecordingMissing
	}
	if p.Local {
		return p, "", nil
	}
	op := "stream"
	if download {
		op = "download"
	}
	token, e := s.control.RecordingCapability(ctx, p.Recording, p.Member, op)
	if e != nil {
		return p, "", e
	}
	if p.Relationship.Mode == "Direct" {
		return p, p.Relationship.Endpoint + "/internal/v1/recordings/" + id + "?token=" + url.QueryEscape(token), nil
	}
	if !nginx {
		return p, "", ErrUnavailable
	}
	origin, e := node.Endpoint(p.Relationship.Endpoint)
	if e != nil {
		return p, "", e
	}
	u, e := url.Parse(origin)
	if e != nil {
		return p, "", e
	}
	port := u.Port()
	if port == "" {
		port = "443"
	}
	return p, "/_relay_recording/" + u.Hostname() + "/" + port + "/" + id + "/" + token, nil
}
func (s *Manager) cleanup(ctx context.Context, user, id string, a store.KaraokeAudit) error {
	p, e := s.placement(ctx, user, id, false)
	if errors.Is(e, store.ErrRecordingMissing) {
		return nil
	}
	if e != nil {
		return e
	}
	if p.Recording.State != "deleting" {
		return store.ErrRecordingState
	}
	if p.Local {
		return s.volume.Delete(ctx, p.Member.ID, user, id, a, store.NodeAudit{})
	}
	if e = s.control.DeleteRecording(ctx, p.Recording, p.Member); e != nil {
		return e
	}
	return s.repo.CompleteRecordingDeletion(ctx, user, id, a)
}
func (s *Manager) Delete(ctx context.Context, user, id string, pendingOnly bool, a store.KaraokeAudit) error {
	unlock, e := s.volume.Lock(id)
	if e != nil {
		return e
	}
	defer unlock()
	v, e := s.repo.StageRecordingDeletion(ctx, user, id, pendingOnly, a)
	if e != nil || v == nil {
		return e
	}
	e = s.cleanup(ctx, user, id, a)
	if e != nil {
		s.repo.DeferRecordingDeletion(ctx, id)
	}
	return e
}
func (s *Manager) RecoverUser(ctx context.Context, user string) error {
	if _, e := s.master(ctx); e != nil {
		return e
	}
	rows, e := s.repo.ListRecordings(ctx, user, "deleting", 500)
	if e != nil {
		return e
	}
	var failures []error
	for _, v := range rows {
		if e = s.Delete(ctx, user, v.ID, false, store.KaraokeAudit{Action: "recording-delete"}); e != nil {
			failures = append(failures, e)
		}
	}
	return errors.Join(failures...)
}
func (s *Manager) Run(ctx context.Context) {
	tick := func() {
		n, e := s.nodes.ReadIdentity(ctx)
		if e != nil {
			slog.Error("recording cleanup identity unavailable", "error", e)
			return
		}
		if e = s.volume.Ready(ctx); e != nil {
			if e = s.volume.Recover(ctx); e != nil {
				slog.Error("recording storage recovery deferred", "error", e)
				return
			}
		}
		if n.Role != "Master" {
			return
		}
		if e = s.repo.StageExpiredRecordings(ctx, 50); e != nil {
			slog.Error("recording expiry deferred", "error", e)
			return
		}
		rows, e := s.repo.PendingRecordingDeletions(ctx, 50)
		if e != nil {
			slog.Error("recording cleanup queue unavailable", "error", e)
			return
		}
		sem := make(chan struct{}, 4)
		var wg sync.WaitGroup
	jobs:
		for _, v := range rows {
			if ctx.Err() != nil {
				break
			}
			select {
			case sem <- struct{}{}:
			case <-ctx.Done():
				break jobs
			}
			wg.Go(func() {
				defer func() { <-sem }()
				job, cancel := context.WithTimeout(ctx, 20*time.Second)
				defer cancel()
				if e := s.Delete(job, v.UserID, v.ID, false, store.KaraokeAudit{Action: "recording-delete"}); e != nil {
					slog.Warn("recording cleanup deferred", "recording_id", v.ID, "error", e)
				}
			})
		}
		wg.Wait()
		users, e := s.accounts.DeletingUsers(ctx, 20)
		if e != nil {
			slog.Error("account deletion queue unavailable", "error", e)
			return
		}
		for _, user := range users {
			if _, e = s.accounts.FinishUserDeletion(ctx, user, store.KaraokeAudit{Action: "account-delete"}); e != nil {
				slog.Warn("account deletion deferred", "user_id", user, "error", e)
			}
		}
	}
	tick()
	timer := time.NewTicker(30 * time.Second)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			tick()
		}
	}
}
