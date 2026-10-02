// Package media implements the public catalog against domain repositories and
// a rooted filesystem. Local object identities persist across file scans.
package media

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io/fs"
	"math"
	"net/url"
	"os"
	"path"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/search"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"golang.org/x/text/cases"
)

var ErrPath = errors.New("Invalid media path")
var ErrCategory = errors.New("Media category not found")
var ErrLyrics = errors.New("Lyrics not found")

type Service struct {
	root             *os.Root
	repository       store.MediaRepository
	identity         *node.Identity
	mutation         sync.RWMutex
	search           *search.Engine
	recoveryRequired bool
	nodes            store.NodeRepository
	pool             store.PoolRepository
	control          *node.Service
	owned            store.OwnedStorageRepository
}

type Category struct {
	Name string `json:"name"`
	URL  string `json:"url"`
}
type Track struct {
	MediaPath  string `json:"media_path"`
	Title      string `json:"title"`
	Artist     string `json:"artist"`
	Type       string `json:"type"`
	URL        string `json:"url"`
	Cover      string `json:"cover"`
	MediaID    string `json:"media_id"`
	ResourceID string `json:"resource_id,omitempty"`
	store.PlaybackStats
	HasLyrics *bool  `json:"has_lyrics,omitempty"`
	KaraokeID string `json:"karaoke_id"`
}

func New(directory string, repo store.MediaRepository, identity *node.Identity) (*Service, error) {
	return NewContext(context.Background(), directory, repo, identity)
}

func NewContext(parent context.Context, directory string, repo store.MediaRepository, identity *node.Identity) (*Service, error) {
	if err := parent.Err(); err != nil {
		return nil, err
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		return nil, err
	}
	service := &Service{root: root, repository: repo, identity: identity}
	service.owned, _ = repo.(store.OwnedStorageRepository)
	service.pool, _ = repo.(store.PoolRepository)
	service.search, err = search.New()
	if err != nil {
		root.Close()
		return nil, err
	}
	// Recovery may hash multi-GiB completed uploads. No HTTP traffic is accepted
	// until durable publication/deletion intents have been reconciled.
	recovery, cancel := context.WithTimeout(parent, 30*time.Minute)
	release, err := service.acquire(recovery, true)
	if err != nil {
		cancel()
		root.Close()
		return nil, err
	}
	defer release()
	err = service.recoverDeletes(recovery)
	if err == nil {
		err = service.recoverUploads(recovery)
	}
	if err == nil {
		err = service.recoverOwnedReservations(recovery)
	}
	if err == nil {
		err = service.recoverRenames(recovery)
	}
	cancel()
	if err != nil {
		root.Close()
		return nil, err
	}
	if err := service.ensureDefaultLyric(); err != nil {
		root.Close()
		return nil, err
	}
	if err := service.root.Remove(".recovery-required"); err != nil && !errors.Is(err, os.ErrNotExist) {
		root.Close()
		return nil, err
	}
	if err := service.syncDirectory("."); err != nil {
		root.Close()
		return nil, err
	}
	return service, nil
}
func (s *Service) Close() error { return s.root.Close() }

func mediaRoot(kind string) string {
	if kind == "music" {
		return "music"
	}
	return "vido"
}
func validExt(root, name string) bool {
	ext := strings.ToLower(path.Ext(name))
	switch root {
	case "music":
		return ext == ".mp3" || ext == ".m4a" || ext == ".flac" || ext == ".wav"
	case "vido":
		return ext == ".mp4" || ext == ".webm" || ext == ".mkv"
	case "lyrics":
		return ext == ".lrc"
	}
	return false
}

// safeInfo forbids symlinks at EVERY component, not only the leaf. os.Root also
// prevents an outside-root escape if the filesystem changes between operations.
func (s *Service) safeInfo(name string) (fs.FileInfo, error) {
	if name == "" || name != path.Clean(name) || strings.HasPrefix(name, "/") || strings.ContainsAny(name, "\\\x00") {
		return nil, ErrPath
	}
	var info fs.FileInfo
	parts := strings.Split(name, "/")
	for i, p := range parts {
		if p == "" || strings.HasPrefix(p, ".") {
			return nil, ErrPath
		}
		var err error
		info, err = s.root.Lstat(strings.Join(parts[:i+1], "/"))
		if err != nil {
			return nil, err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return nil, ErrPath
		}
	}
	return info, nil
}

func (s *Service) ValidateCategory(name, kind string) error {
	parts := strings.Split(name, "/")
	if (kind != "music" && kind != "video") || len(parts) < 2 || len(parts) > 3 || parts[0] != mediaRoot(kind) {
		return ErrCategory
	}
	info, err := s.safeInfo(name)
	if err != nil || !info.IsDir() {
		return ErrCategory
	}
	return nil
}

func (s *Service) ValidateTrack(name string) (store.MediaObject, error) {
	name = strings.TrimLeft(strings.ReplaceAll(name, "\\", "/"), "/")
	parts := strings.Split(name, "/")
	if len(parts) < 3 || len(parts) > 4 || (parts[0] != "music" && parts[0] != "vido") || !validExt(parts[0], name) {
		return store.MediaObject{}, ErrPath
	}
	info, err := s.safeInfo(name)
	if errors.Is(err, os.ErrNotExist) {
		return store.MediaObject{}, os.ErrNotExist
	}
	if err != nil || !info.Mode().IsRegular() {
		return store.MediaObject{}, ErrPath
	}
	kind := "audio"
	if parts[0] == "vido" {
		kind = "video"
	}
	return store.MediaObject{Path: name, Kind: kind}, nil
}

func hidden(name string, values map[string]bool) bool {
	for {
		if values[name] {
			return true
		}
		i := strings.LastIndexByte(name, '/')
		if i < 0 {
			return false
		}
		name = name[:i]
	}
}
func (s *Service) hiddenPaths(ctx context.Context, include bool) (map[string]bool, error) {
	if include {
		return map[string]bool{}, nil
	}
	return s.repository.HiddenPaths(ctx)
}

func (s *Service) readDir(name string) ([]fs.DirEntry, error) {
	if info, err := s.safeInfo(name); err != nil || !info.IsDir() {
		if err != nil {
			return nil, err
		}
		return nil, ErrCategory
	}
	f, err := s.root.Open(name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return f.ReadDir(-1)
}

func (s *Service) hasDirect(name, root string, h map[string]bool) (bool, error) {
	entries, err := s.readDir(name)
	if err != nil {
		return false, err
	}
	for _, entry := range entries {
		if entry.Type().IsRegular() && !strings.HasPrefix(entry.Name(), ".") && validExt(root, entry.Name()) && !hidden(name+"/"+entry.Name(), h) {
			return true, nil
		}
	}
	return false, nil
}

func categoryURL(kind, name string, include bool) string {
	values := url.Values{"path": {name}}
	if include {
		values.Set("include_hidden", "true")
	}
	return "/api/v1/media/" + kind + "/category?" + values.Encode()
}

func (s *Service) categories(ctx context.Context, kind, parent string, include bool) ([]Category, error) {
	h, err := s.hiddenPaths(ctx, include)
	if err != nil {
		return nil, err
	}
	entries, err := s.readDir(parent)
	if err != nil {
		return nil, err
	}
	result := []Category{}
	for _, entry := range entries {
		if !entry.IsDir() || entry.Type()&os.ModeSymlink != 0 || strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		name := parent + "/" + entry.Name()
		if hidden(name, h) {
			continue
		}
		has, err := s.hasDirect(name, mediaRoot(kind), h)
		if err != nil {
			return nil, err
		}
		if !has && parent == mediaRoot(kind) {
			children, err := s.readDir(name)
			if err != nil {
				return nil, err
			}
			for _, child := range children {
				if child.IsDir() && child.Type()&os.ModeSymlink == 0 && !strings.HasPrefix(child.Name(), ".") && !hidden(name+"/"+child.Name(), h) {
					found, err := s.hasDirect(name+"/"+child.Name(), mediaRoot(kind), h)
					if err != nil {
						return nil, err
					}
					has = has || found
				}
			}
		}
		if has {
			result = append(result, Category{entry.Name(), categoryURL(kind, name, include)})
		}
	}
	preferences, err := s.repository.DirectoryPreferences(ctx)
	if err != nil {
		return nil, err
	}
	fold := cases.Fold()
	sort.SliceStable(result, func(i, j int) bool {
		a, b := result[i], result[j]
		pa, pb := preferences[parent+"/"+a.Name], preferences[parent+"/"+b.Name]
		if pa != pb {
			return pa > pb
		}
		return fold.String(a.Name) < fold.String(b.Name)
	})
	return result, nil
}

func (s *Service) Categories(ctx context.Context, kind string, include bool) ([]Category, error) {
	if kind != "music" && kind != "video" {
		return nil, ErrCategory
	}
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return nil, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return nil, err
	}
	role, err := s.role(ctx)
	if err != nil {
		return nil, err
	}
	if role == "Follower" {
		return []Category{}, nil
	}
	if role == "Master" {
		return s.globalCategories(ctx, kind, mediaRoot(kind), include)
	}
	return s.categories(ctx, kind, mediaRoot(kind), include)
}
func (s *Service) Subcategories(ctx context.Context, kind, name string, include bool) ([]Category, error) {
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return nil, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return nil, err
	}
	role, err := s.role(ctx)
	if err != nil {
		return nil, err
	}
	if role == "Follower" {
		return nil, ErrCategory
	}
	if role == "Master" {
		if err := s.globalCategory(ctx, name, kind); err != nil {
			return nil, err
		}
		if len(strings.Split(name, "/")) != 2 {
			return []Category{}, nil
		}
		return s.globalCategories(ctx, kind, name, include)
	}
	if err := s.ValidateCategory(name, kind); err != nil {
		return nil, err
	}
	if len(strings.Split(name, "/")) != 2 {
		return []Category{}, nil
	}
	return s.categories(ctx, kind, name, include)
}

func (s *Service) Catalog(ctx context.Context, kind, name, session string, include bool) ([]Track, error) {
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return nil, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return nil, err
	}
	role, err := s.role(ctx)
	if err != nil {
		return nil, err
	}
	if role == "Follower" {
		return nil, ErrCategory
	}
	if role == "Master" {
		return s.globalCatalog(ctx, kind, name, session, include)
	}
	if err := s.ValidateCategory(name, kind); err != nil {
		return nil, err
	}
	h, err := s.hiddenPaths(ctx, include)
	if err != nil {
		return nil, err
	}
	files, err := s.readDir(name)
	if err != nil {
		return nil, err
	}
	items := []Track{}
	objects := []store.MediaObject{}
	objectKind := "audio"
	if kind == "video" {
		objectKind = "video"
	}
	for _, entry := range files {
		if !entry.Type().IsRegular() || strings.HasPrefix(entry.Name(), ".") || !validExt(mediaRoot(kind), entry.Name()) {
			continue
		}
		file := name + "/" + entry.Name()
		if hidden(file, h) {
			continue
		}
		objects = append(objects, store.MediaObject{Path: file, Kind: objectKind})
		items = append(items, Track{MediaPath: file, Title: strings.TrimSuffix(entry.Name(), path.Ext(entry.Name())), Artist: "前沿娱乐", Type: objectKind, URL: "/api/v1/media/stream?file_path=" + quotePath(file), Cover: "/favicon.ico"})
	}
	ids, err := s.repository.EnsureObjects(ctx, objects)
	if err != nil {
		return nil, err
	}
	all := make([]string, 0, len(ids))
	for _, id := range ids {
		all = append(all, id)
	}
	stats, err := s.repository.Stats(ctx, all)
	if err != nil {
		return nil, err
	}
	for i := range items {
		item := &items[i]
		item.MediaID = ids[item.MediaPath]
		item.PlaybackStats = stats[item.MediaID]
		if kind == "music" {
			if _, err := s.lyricFor(ctx, store.MediaObject{ID: item.MediaID, Path: item.MediaPath, Kind: "audio"}); err != nil {
				return nil, err
			}
			yes := true
			item.HasLyrics = &yes
		}
		item.KaraokeID, err = s.identity.KaraokeHandle(item.MediaID, false)
		if err != nil {
			return nil, err
		}
	}
	sort.SliceStable(items, func(i, j int) bool {
		if items[i].Preference != items[j].Preference {
			return items[i].Preference > items[j].Preference
		}
		return randomKey(session, items[i].MediaID) < randomKey(session, items[j].MediaID)
	})
	return items, nil
}

func randomKey(session, id string) string {
	sum := sha256.Sum256([]byte(session + ":" + id))
	return hex.EncodeToString(sum[:])
}
func QuotePath(name string) string {
	parts := strings.Split(name, "/")
	for i := range parts {
		parts[i] = strings.ReplaceAll(url.QueryEscape(parts[i]), "+", "%20")
	}
	return strings.Join(parts, "/")
}

func quotePath(name string) string { return QuotePath(name) }

type Stream struct {
	File       *os.File
	Info       fs.FileInfo
	Path       string
	ObjectID   string
	OwnerID    string
	ResourceID string
}

func (s *Service) Stream(ctx context.Context, name string) (*Stream, error) {
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return nil, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return nil, err
	}
	o, err := s.ValidateTrack(name)
	if err != nil {
		return nil, err
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
	ids, err := s.repository.EnsureObjects(ctx, []store.MediaObject{o})
	if err != nil {
		f.Close()
		return nil, err
	}
	id := ids[o.Path]
	identity, err := s.IdentityState(ctx)
	if err != nil {
		f.Close()
		return nil, err
	}
	return &Stream{f, info, o.Path, id, identity.ID, node.ResourceID(identity.ID, id)}, nil
}

func (s *Service) Playback(ctx context.Context, name, session string, played, duration float64) (store.PlaybackResult, error) {
	role, roleErr := s.role(ctx)
	if roleErr != nil {
		return store.PlaybackResult{}, roleErr
	}
	if role == "Master" {
		return s.GlobalPlayback(ctx, name, "", session, played, duration)
	}
	if role == "Follower" {
		return store.PlaybackResult{}, os.ErrNotExist
	}
	if math.IsNaN(played) || math.IsNaN(duration) || math.IsInf(played, 0) || math.IsInf(duration, 0) || played <= 0 || played > 86400 || duration <= 0 || duration > 86400 {
		return store.PlaybackResult{}, errors.New("Invalid media duration")
	}
	canonical, err := normalizeSession(session)
	if err != nil {
		return store.PlaybackResult{}, err
	}
	threshold := math.Max(5, math.Min(30, duration*0.5))
	if played+0.05 < threshold {
		return store.PlaybackResult{}, errors.New("Playback threshold not reached")
	}
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return store.PlaybackResult{}, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return store.PlaybackResult{}, err
	}
	o, err := s.ValidateTrack(name)
	if err != nil {
		return store.PlaybackResult{}, err
	}
	result, err := s.repository.RecordPlayback(ctx, o, canonical)
	result.Threshold = threshold
	return result, err
}

func normalizeSession(value string) (string, error) {
	value = strings.TrimPrefix(strings.ToLower(value), "urn:uuid:")
	value = strings.Trim(value, "{}")
	raw := strings.ReplaceAll(value, "-", "")
	decoded, err := hex.DecodeString(raw)
	if err != nil || len(decoded) != 16 {
		return "", errors.New("Invalid playback session")
	}
	return raw[:8] + "-" + raw[8:12] + "-" + raw[12:16] + "-" + raw[16:20] + "-" + raw[20:], nil
}
