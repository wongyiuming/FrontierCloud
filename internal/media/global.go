package media

import (
	"context"
	"errors"
	"io"
	"math"
	"net/url"
	"os"
	"path"
	"sort"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"golang.org/x/text/cases"
)

var ErrUnavailable = errors.New("Media temporarily unavailable")

// ConfigureCluster is startup-only; role decisions always use durable state,
// never the identity snapshot created before a role promotion.
func (s *Service) ConfigureCluster(nodes store.NodeRepository, pool store.PoolRepository, control *node.Service) {
	s.nodes, s.pool, s.control = nodes, pool, control
}
func (s *Service) role(ctx context.Context) (string, error) {
	if s.nodes == nil {
		return "Standalone", nil
	}
	row, err := s.nodes.ReadIdentity(ctx)
	return row.Role, err
}
func categoryShape(name, kind string) bool {
	parts := strings.Split(name, "/")
	if (kind != "music" && kind != "video") || len(parts) < 2 || len(parts) > 3 || parts[0] != mediaRoot(kind) || strings.ContainsAny(name, "\\\x00") || name != path.Clean(name) {
		return false
	}
	for _, part := range parts {
		if part == "" || strings.HasPrefix(part, ".") {
			return false
		}
	}
	return true
}
func (s *Service) globalCategory(ctx context.Context, name, kind string) error {
	if !categoryShape(name, kind) {
		return ErrCategory
	}
	rows, err := s.pool.Resources(ctx, name, false)
	if err != nil {
		return err
	}
	if len(rows) == 0 {
		return ErrCategory
	}
	return nil
}
func (s *Service) CategoryExists(ctx context.Context, name, kind string) error {
	release, err := s.acquire(ctx, false)
	if err != nil {
		return err
	}
	defer release()
	if err = s.ready(); err != nil {
		return err
	}
	role, err := s.role(ctx)
	if err != nil {
		return err
	}
	if role == "Master" {
		return s.globalCategory(ctx, name, kind)
	}
	if role == "Follower" {
		return ErrCategory
	}
	return s.ValidateCategory(name, kind)
}
func (s *Service) globalCategories(ctx context.Context, kind, parent string, include bool) ([]Category, error) {
	rows, err := s.pool.Resources(ctx, parent, false)
	if err != nil {
		return nil, err
	}
	h, err := s.hiddenPaths(ctx, include)
	if err != nil {
		return nil, err
	}
	names := map[string]bool{}
	for _, row := range rows {
		if hidden(row.Path, h) || !strings.HasPrefix(row.Path, parent+"/") {
			continue
		}
		parts := strings.Split(strings.TrimPrefix(row.Path, parent+"/"), "/")
		if len(parts) >= 2 && categoryShape(parent+"/"+parts[0], kind) {
			names[parts[0]] = true
		}
	}
	result := []Category{}
	for name := range names {
		result = append(result, Category{name, categoryURL(kind, parent+"/"+name, include)})
	}
	preferences, err := s.repository.DirectoryPreferences(ctx)
	if err != nil {
		return nil, err
	}
	fold := cases.Fold()
	sort.Slice(result, func(i, j int) bool {
		a, b := result[i].Name, result[j].Name
		pa, pb := preferences[parent+"/"+a], preferences[parent+"/"+b]
		if pa != pb {
			return pa > pb
		}
		fa, fb := fold.String(a), fold.String(b)
		if fa == fb {
			return a < b
		}
		return fa < fb
	})
	return result, nil
}
func (s *Service) globalCatalog(ctx context.Context, kind, name, session string, include bool) ([]Track, error) {
	if err := s.globalCategory(ctx, name, kind); err != nil {
		return nil, err
	}
	rows, err := s.pool.Resources(ctx, name, false)
	if err != nil {
		return nil, err
	}
	h, err := s.hiddenPaths(ctx, include)
	if err != nil {
		return nil, err
	}
	items := []Track{}
	for _, row := range rows {
		if path.Dir(row.Path) != name || hidden(row.Path, h) {
			continue
		}
		handle, err := s.identity.KaraokeHandle(row.ID, true)
		if err != nil {
			return nil, err
		}
		params := url.Values{"file_path": {row.Path}, "resource_id": {row.ID}}
		item := Track{MediaPath: row.Path, Title: strings.TrimSuffix(path.Base(row.Path), path.Ext(row.Path)), Artist: "前沿娱乐", Type: row.Kind, URL: "/api/v1/media/stream?" + params.Encode(), Cover: "/favicon.ico", MediaID: row.ID, ResourceID: row.ID, PlaybackStats: row.PlaybackStats, KaraokeID: handle}
		has := row.HasLyrics
		item.HasLyrics = &has
		items = append(items, item)
	}
	sort.SliceStable(items, func(i, j int) bool {
		if items[i].Preference != items[j].Preference {
			return items[i].Preference > items[j].Preference
		}
		return randomKey(session, items[i].MediaID) < randomKey(session, items[j].MediaID)
	})
	return items, nil
}

func (s *Service) resolveGlobal(ctx context.Context, id, name string) (store.GlobalMedia, *store.Relationship, error) {
	var row store.GlobalMedia
	var err error
	if id == "" {
		rows, e := s.pool.Resources(ctx, name, true)
		if e != nil {
			return row, nil, e
		}
		if len(rows) != 1 {
			return row, nil, os.ErrNotExist
		}
		row = rows[0]
	} else {
		row, err = s.pool.Resource(ctx, id)
		if errors.Is(err, store.ErrNodeState) {
			err = os.ErrNotExist
		}
		if err != nil {
			return row, nil, err
		}
	}
	if name != "" && name != row.Path {
		return row, nil, os.ErrNotExist
	}
	var relation *store.Relationship
	if row.RelationshipID != nil {
		v, e := s.nodes.Relationship(ctx, *row.RelationshipID)
		if e != nil {
			return row, nil, e
		}
		if v.State != "active" || v.Direction != "downstream" || v.PeerID != row.MemberID {
			return row, nil, os.ErrNotExist
		}
		relation = &v
		if v.Status == "offline" || time.Now().Unix()-v.LastHeartbeat >= 120 {
			return row, relation, ErrUnavailable
		}
	} else if row.MemberID != s.identity.ID {
		return row, nil, os.ErrNotExist
	}
	if row.Health != "online" {
		return row, relation, ErrUnavailable
	}
	return row, relation, nil
}

type Delivery struct {
	Stream                        *Stream
	Redirect                      string
	Relay                         string
	ResourceID, OwnerID, ObjectID string
}

func (s *Service) Delivery(ctx context.Context, name, id, requestID, traceID string, nginx bool) (Delivery, error) {
	role, err := s.role(ctx)
	if err != nil {
		return Delivery{}, err
	}
	if role == "Follower" {
		return Delivery{}, os.ErrNotExist
	}
	if role != "Master" {
		if id != "" {
			return Delivery{}, ErrUnavailable
		}
		local, err := s.Stream(ctx, name)
		if err != nil {
			return Delivery{}, err
		}
		return Delivery{Stream: local, ResourceID: local.ResourceID, OwnerID: local.OwnerID, ObjectID: local.ObjectID}, nil
	}
	row, relation, err := s.resolveGlobal(ctx, id, name)
	if err != nil {
		return Delivery{}, err
	}
	result := Delivery{ResourceID: row.ID, OwnerID: row.MemberID, ObjectID: row.ObjectID}
	if relation == nil {
		local, err := s.OwnedStream(ctx, row.ObjectID)
		if err != nil {
			return Delivery{}, err
		}
		if local.Path != row.Path {
			local.File.Close()
			return Delivery{}, os.ErrNotExist
		}
		result.Stream = local
		return result, nil
	}
	token, err := s.control.MediaCapability(ctx, relation.ID, row.MemberID, row.ObjectID, row.ID, requestID, traceID)
	if err != nil {
		return Delivery{}, err
	}
	origin, err := node.Endpoint(relation.Endpoint)
	if err != nil {
		return Delivery{}, err
	}
	if relation.Mode == "Direct" {
		result.Redirect = origin + "/internal/v1/media/" + row.ObjectID + "?" + url.Values{"token": {token}}.Encode()
		return result, nil
	}
	if relation.Mode != "Relay" || !nginx {
		return Delivery{}, ErrUnavailable
	}
	parsed, err := url.Parse(origin)
	if err != nil {
		return Delivery{}, err
	}
	port := parsed.Port()
	if port == "" {
		port = "443"
	}
	result.Relay = "/_relay_media/" + parsed.Hostname() + "/" + port + "/" + row.ObjectID + "/" + token
	return result, nil
}

// OwnedStream only reads a durable local object. It cannot create a new ID or
// map a caller-supplied path to a different placement.
func (s *Service) OwnedStream(ctx context.Context, id string) (*Stream, error) {
	release, err := s.acquire(ctx, false)
	if err != nil {
		return nil, err
	}
	defer release()
	if err = s.ready(); err != nil {
		return nil, err
	}
	o, err := s.repository.ObjectByID(ctx, id)
	if err != nil {
		return nil, err
	}
	if o == nil {
		return nil, os.ErrNotExist
	}
	checked, err := s.ValidateTrack(o.Path)
	if err != nil {
		return nil, err
	}
	if checked.Kind != o.Kind {
		return nil, ErrPath
	}
	f, err := s.root.Open(o.Path)
	if err != nil {
		return nil, err
	}
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		f.Close()
		return nil, ErrPath
	}
	return &Stream{File: f, Info: info, Path: o.Path, ObjectID: o.ID, OwnerID: s.identity.ID, ResourceID: node.ResourceID(s.identity.ID, o.ID)}, nil
}

func (s *Service) GlobalPlayback(ctx context.Context, name, id, session string, played, duration float64) (store.PlaybackResult, error) {
	role, err := s.role(ctx)
	if err != nil {
		return store.PlaybackResult{}, err
	}
	if role != "Master" {
		return store.PlaybackResult{}, ErrUnavailable
	}
	if math.IsNaN(played) || math.IsNaN(duration) || math.IsInf(played, 0) || math.IsInf(duration, 0) || played <= 0 || duration <= 0 || played > 86400 || duration > 86400 {
		return store.PlaybackResult{}, errors.New("Invalid media duration")
	}
	canonical, err := normalizeSession(session)
	if err != nil {
		return store.PlaybackResult{}, err
	}
	threshold := math.Max(5, math.Min(30, duration*.5))
	if played+.05 < threshold {
		return store.PlaybackResult{}, errors.New("Playback threshold not reached")
	}
	row, _, err := s.resolveGlobal(ctx, id, name)
	if err != nil {
		return store.PlaybackResult{}, err
	}
	result, err := s.pool.RecordGlobalPlayback(ctx, row.ID, canonical)
	result.Threshold = threshold
	return result, err
}

func (s *Service) LyricsResource(ctx context.Context, name, id string) ([]LyricEntry, error) {
	role, err := s.role(ctx)
	if err != nil {
		return nil, err
	}
	if role == "Follower" {
		return nil, ErrLyrics
	}
	if role != "Master" {
		if id != "" {
			return nil, ErrUnavailable
		}
		return s.Lyrics(ctx, name)
	}
	row, _, err := s.resolveGlobal(ctx, id, name)
	if err != nil {
		return nil, err
	}
	if row.Kind != "audio" {
		return nil, ErrLyrics
	}
	release, err := s.acquire(ctx, false)
	if err != nil {
		return nil, err
	}
	defer release()
	if err = s.ready(); err != nil {
		return nil, err
	}
	lyric, err := s.repository.LyricPath(ctx, row.ID)
	if err != nil {
		return nil, err
	}
	if !s.validLyric(lyric) {
		if err = s.ensureDefaultLyric(); err != nil {
			return nil, err
		}
		if err = s.pool.BindGlobalLyric(ctx, row.ID, store.MediaObject{Kind: "lyric", Path: defaultLyric}); err != nil {
			return nil, err
		}
		lyric = defaultLyric
	}
	f, err := s.root.Open(lyric)
	if err != nil {
		return nil, ErrLyrics
	}
	defer f.Close()
	payload, err := io.ReadAll(io.LimitReader(f, maxLyricBytes+1))
	if err != nil {
		return nil, err
	}
	if len(payload) > maxLyricBytes {
		return nil, ErrLyrics
	}
	entries, err := ParseLRC(payload)
	if err != nil {
		return nil, ErrLyrics
	}
	return entries, nil
}
