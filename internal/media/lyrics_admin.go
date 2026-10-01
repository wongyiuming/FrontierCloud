package media

import (
	"context"
	"encoding/json"
	"path"
	"sort"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/search"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"golang.org/x/text/cases"
)

type LyricCatalogItem struct {
	Path        string  `json:"path"`
	Name        string  `json:"name"`
	MediaID     string  `json:"media_id,omitempty"`
	LyricID     string  `json:"lyric_id,omitempty"`
	HasLyrics   *bool   `json:"has_lyrics,omitempty"`
	LyricPath   *string `json:"lyric_path,omitempty"`
	LinkedCount *int    `json:"linked_count,omitempty"`
}

func (item LyricCatalogItem) MarshalJSON() ([]byte, error) {
	value := map[string]any{"path": item.Path, "name": item.Name}
	if item.MediaID != "" {
		value["media_id"] = item.MediaID
		value["has_lyrics"] = item.HasLyrics
		value["lyric_path"] = item.LyricPath
	} else {
		value["lyric_id"] = item.LyricID
		value["linked_count"] = item.LinkedCount
	}
	return json.Marshal(value)
}

type LyricCatalog struct {
	Scopes           map[string]string     `json:"scopes"`
	Counts           map[string]int        `json:"counts"`
	Truncated        map[string]bool       `json:"truncated"`
	TrackDirectories []PriorityDirectory   `json:"track_directories"`
	LyricDirectories []PriorityDirectory   `json:"lyric_directories"`
	Tracks           []LyricCatalogItem    `json:"tracks"`
	Lyrics           []LyricCatalogItem    `json:"lyrics"`
	Relations        []store.LyricRelation `json:"relations"`
}

func (s *Service) lyricScope(name, root string) (string, error) {
	name, err := normalizeAdminPath(name, false)
	if err != nil || strings.Split(name, "/")[0] != root {
		return "", ErrPath
	}
	info, err := s.safeInfo(name)
	if err != nil {
		return "", err
	}
	if !info.IsDir() {
		return "", ErrPath
	}
	return name, nil
}
func (s *Service) lyricCatalogScan(ctx context.Context, scope, query, kind string) ([]LyricCatalogItem, []PriorityDirectory, int, bool, error) {
	items, err := s.scan(ctx, scope, nil)
	if err != nil {
		return nil, nil, 0, false, err
	}
	files := []LyricCatalogItem{}
	dirs := []PriorityDirectory{}
	normalized := ""
	if query != "" {
		normalized, err = search.Query(query)
		if err != nil {
			return nil, nil, 0, false, err
		}
	}
	if normalized == "" {
		entries, err := s.readDir(scope)
		if err != nil {
			return nil, nil, 0, false, err
		}
		for _, entry := range entries {
			name := scope + "/" + entry.Name()
			if !entry.IsDir() || strings.HasPrefix(entry.Name(), ".") || len(strings.Split(name, "/")) > 3 {
				continue
			}
			count := 0
			for _, item := range items {
				if strings.HasPrefix(item.Path, name+"/") {
					count++
				}
			}
			dirs = append(dirs, PriorityDirectory{Name: entry.Name(), Path: name, Count: count})
		}
	}
	for _, item := range items {
		name := strings.TrimSuffix(path.Base(item.Path), path.Ext(item.Path))
		if normalized != "" {
			aliases, err := s.search.Text(name, item.Path)
			if err != nil {
				return nil, nil, 0, false, err
			}
			if !strings.Contains(aliases, normalized) {
				continue
			}
		} else if path.Dir(item.Path) != scope {
			continue
		}
		files = append(files, LyricCatalogItem{Path: item.Path, Name: name})
	}
	fold := cases.Fold()
	sort.Slice(files, func(i, j int) bool {
		a, b := fold.String(files[i].Name), fold.String(files[j].Name)
		if a == b {
			return fold.String(files[i].Path) < fold.String(files[j].Path)
		}
		return a < b
	})
	sort.Slice(dirs, func(i, j int) bool { return fold.String(dirs[i].Name) < fold.String(dirs[j].Name) })
	truncated := len(files) > 200
	if truncated {
		files = files[:200]
	}
	objects := make([]store.MediaObject, 0, len(files))
	for _, file := range files {
		objects = append(objects, store.MediaObject{Path: file.Path, Kind: kind})
	}
	ids, err := s.repository.EnsureObjects(ctx, objects)
	if err != nil {
		return nil, nil, 0, false, err
	}
	for i := range files {
		if kind == "audio" {
			files[i].MediaID = ids[files[i].Path]
		} else {
			files[i].LyricID = ids[files[i].Path]
		}
	}
	return files, dirs, len(items), truncated, nil
}
func (s *Service) LyricCatalog(ctx context.Context, trackScope, lyricScope, trackQuery, lyricQuery string) (LyricCatalog, error) {
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return LyricCatalog{}, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return LyricCatalog{}, err
	}
	trackScope, err := s.lyricScope(trackScope, "music")
	if err != nil {
		return LyricCatalog{}, err
	}
	lyricScope, err = s.lyricScope(lyricScope, "lyrics")
	if err != nil {
		return LyricCatalog{}, err
	}
	tracks, trackDirs, trackTotal, trackTruncated, err := s.lyricCatalogScan(ctx, trackScope, trackQuery, "audio")
	if err != nil {
		return LyricCatalog{}, err
	}
	lyrics, lyricDirs, lyricTotal, lyricTruncated, err := s.lyricCatalogScan(ctx, lyricScope, lyricQuery, "lyric")
	if err != nil {
		return LyricCatalog{}, err
	}
	relations, err := s.repository.LyricRelations(ctx, trackScope, lyricScope)
	if err != nil {
		return LyricCatalog{}, err
	}
	trackSet, lyricSet := map[string]bool{}, map[string]bool{}
	for _, track := range tracks {
		trackSet[track.Path] = true
	}
	for _, lyric := range lyrics {
		// Match the reference auto-link glob, independently of the catalog's
		// case-insensitive supported-extension validation.
		if !strings.HasSuffix(lyric.Path, ".lrc") {
			continue
		}
		lyricSet[lyric.Path] = true
	}
	visible := []store.LyricRelation{}
	byTrack := map[string]string{}
	counts := map[string]int{}
	relationTotal := 0
	for _, relation := range relations {
		if relation.Lyric == defaultLyric {
			continue
		}
		if strings.HasPrefix(relation.Track, trackScope+"/") && strings.HasPrefix(relation.Lyric, lyricScope+"/") {
			relationTotal++
		}
		if trackSet[relation.Track] || lyricSet[relation.Lyric] {
			visible = append(visible, relation)
			byTrack[relation.Track] = relation.Lyric
			counts[relation.Lyric]++
		}
	}
	for i := range tracks {
		value, ok := byTrack[tracks[i].Path]
		tracks[i].HasLyrics = new(ok)
		if ok {
			tracks[i].LyricPath = new(value)
		}
	}
	for i := range lyrics {
		lyrics[i].LinkedCount = new(counts[lyrics[i].Path])
	}
	return LyricCatalog{Scopes: map[string]string{"track": trackScope, "lyric": lyricScope}, Counts: map[string]int{"tracks": trackTotal, "lyrics": lyricTotal, "relations": relationTotal}, Truncated: map[string]bool{"track": trackTruncated, "lyric": lyricTruncated}, TrackDirectories: trackDirs, LyricDirectories: lyricDirs, Tracks: tracks, Lyrics: lyrics, Relations: visible}, nil
}
func (s *Service) ReplaceLyrics(ctx context.Context, originKind, originPath string, linked []string, audit store.AdminAudit) (int, error) {
	release, leaseErr := s.acquire(ctx, true)
	if leaseErr != nil {
		return 0, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return 0, err
	}
	if originKind != "track" && originKind != "lyric" {
		return 0, ErrPath
	}
	validate := func(name string, track bool) (store.MediaObject, error) {
		if track {
			object, err := s.ValidateTrack(name)
			if err != nil || object.Kind != "audio" {
				return store.MediaObject{}, ErrPath
			}
			return object, nil
		}
		if !s.validLyric(name) {
			return store.MediaObject{}, ErrPath
		}
		return store.MediaObject{Path: name, Kind: "lyric"}, nil
	}
	origin, err := validate(originPath, originKind == "track")
	if err != nil || originKind == "lyric" && originPath == defaultLyric {
		return 0, ErrPath
	}
	seen := map[string]bool{}
	targets := []store.MediaObject{}
	for _, name := range linked {
		if seen[name] {
			continue
		}
		object, err := validate(name, originKind != "track")
		if err != nil {
			return 0, err
		}
		seen[name] = true
		targets = append(targets, object)
	}
	if originKind == "track" && len(targets) > 1 {
		return 0, ErrPath
	}
	if err := s.ensureDefaultLyric(); err != nil {
		return 0, err
	}
	return s.repository.ReplaceLyricRelations(ctx, origin, targets, store.MediaObject{Path: defaultLyric, Kind: "lyric"}, audit)
}
func (s *Service) AutoLyrics(ctx context.Context, audit store.AdminAudit) (store.AutoLyricResult, error) {
	release, leaseErr := s.acquire(ctx, true)
	if leaseErr != nil {
		return store.AutoLyricResult{}, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return store.AutoLyricResult{}, err
	}
	tracks, err := s.scan(ctx, "music", nil)
	if err != nil {
		return store.AutoLyricResult{}, err
	}
	lyrics, err := s.scan(ctx, "lyrics", nil)
	if err != nil {
		return store.AutoLyricResult{}, err
	}
	byStem := map[string][]string{}
	byRelative := map[string]string{}
	for _, lyric := range lyrics {
		stem := strings.TrimSuffix(path.Base(lyric.Path), path.Ext(lyric.Path))
		relative := strings.TrimPrefix(strings.TrimSuffix(lyric.Path, path.Ext(lyric.Path)), "lyrics/")
		byStem[stem] = append(byStem[stem], lyric.Path)
		byRelative[relative] = lyric.Path
	}
	result := store.AutoLyricResult{}
	pairs := []store.LyricPair{}
	for _, track := range tracks {
		relative := strings.TrimPrefix(strings.TrimSuffix(track.Path, path.Ext(track.Path)), "music/")
		lyric := byRelative[relative]
		if lyric == "" {
			candidates := byStem[strings.TrimSuffix(path.Base(track.Path), path.Ext(track.Path))]
			if len(candidates) == 1 {
				lyric = candidates[0]
			} else if len(candidates) > 1 {
				result.Ambiguous++
				continue
			} else {
				result.Unmatched++
				continue
			}
		}
		pairs = append(pairs, store.LyricPair{Track: store.MediaObject{Path: track.Path, Kind: "audio"}, Lyric: store.MediaObject{Path: lyric, Kind: "lyric"}})
	}
	return s.repository.AutoLyricRelations(ctx, pairs, result, audit)
}
