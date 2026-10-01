package media

import (
	"context"
	"path"
	"sort"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/search"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"golang.org/x/text/cases"
)

type SearchResult struct {
	Path      string     `json:"path"`
	Query     string     `json:"query"`
	Items     []TreeItem `json:"items"`
	Truncated bool       `json:"truncated"`
}

// scan never follows symlinks or descends outside the supported hierarchy.
// Caller holds the media mutation lock for a coherent filesystem/DB snapshot.
func (s *Service) scan(ctx context.Context, scope string, h map[string]bool) ([]TreeItem, error) {
	if info, err := s.safeInfo(scope); err != nil {
		return nil, err
	} else if !info.IsDir() {
		return nil, ErrCategory
	}
	items := []TreeItem{}
	var visit func(string) error
	visit = func(dir string) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		entries, err := s.readDir(dir)
		if err != nil {
			return err
		}
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
		for _, entry := range entries {
			if err := ctx.Err(); err != nil {
				return err
			}
			if strings.HasPrefix(entry.Name(), ".") {
				continue
			}
			name := dir + "/" + entry.Name()
			parts := strings.Split(name, "/")
			if entry.IsDir() {
				if len(parts) <= 3 {
					if err := visit(name); err != nil {
						return err
					}
				}
				continue
			}
			if !entry.Type().IsRegular() || !validExt(parts[0], name) || name == defaultLyric {
				continue
			}
			if parts[0] != "lyrics" && len(parts) < 3 {
				continue
			}
			info, err := entry.Info()
			if err != nil {
				return err
			}
			size := info.Size()
			items = append(items, TreeItem{Name: entry.Name(), Path: name, Kind: "file", Size: &size, Hidden: hidden(name, h), HiddenDirect: h[name], Media: parts[0] != "lyrics"})
		}
		return nil
	}
	if err := visit(scope); err != nil {
		return nil, err
	}
	return items, nil
}

func (s *Service) Search(ctx context.Context, query, scope string) (SearchResult, error) {
	normalized, err := search.Query(query)
	if err != nil {
		return SearchResult{}, err
	}
	name, err := normalizeAdminPath(scope, false)
	if err != nil {
		return SearchResult{}, ErrPath
	}
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return SearchResult{}, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return SearchResult{}, err
	}
	h, err := s.repository.HiddenPaths(ctx)
	if err != nil {
		return SearchResult{}, err
	}
	items, err := s.scan(ctx, name, h)
	if err != nil {
		return SearchResult{}, err
	}
	result := SearchResult{Path: name, Query: query, Items: []TreeItem{}}
	for _, item := range items {
		text, err := s.search.Text(item.Name, item.Path)
		if err != nil {
			return SearchResult{}, err
		}
		if strings.Contains(text, normalized) {
			if len(result.Items) == 200 {
				result.Truncated = true
				break
			}
			result.Items = append(result.Items, item)
		}
	}
	return result, nil
}

type PriorityItem struct {
	MediaPath string `json:"media_path"`
	Title     string `json:"title"`
	Type      string `json:"type"`
	Hidden    bool   `json:"hidden"`
	MediaID   string `json:"media_id"`
	store.PlaybackStats
}
type PriorityDirectory struct {
	Name  string `json:"name"`
	Path  string `json:"path"`
	Count int    `json:"count"`
}
type Pagination struct {
	Page     int `json:"page"`
	Pages    int `json:"pages"`
	Total    int `json:"total"`
	PageSize int `json:"page_size"`
}
type PriorityResult struct {
	Items        []PriorityItem      `json:"items"`
	Directories  []PriorityDirectory `json:"directories"`
	Scope        string              `json:"scope"`
	Parent       string              `json:"parent"`
	CatalogTotal int                 `json:"catalog_total"`
	Searching    bool                `json:"searching"`
	Minimum      int                 `json:"minimum"`
	Maximum      int                 `json:"maximum"`
	Pagination   Pagination          `json:"pagination"`
}

func (s *Service) Priorities(ctx context.Context, scope, query, kind string, page, pageSize int) (PriorityResult, error) {
	if page < 1 || pageSize < 1 || pageSize > 100 || (kind != "" && kind != "audio" && kind != "video") {
		return PriorityResult{}, ErrPath
	}
	name, err := normalizeAdminPath(scope, true)
	if err != nil {
		return PriorityResult{}, err
	}
	if name != "" && strings.Split(name, "/")[0] == "lyrics" {
		return PriorityResult{}, ErrPath
	}
	normalized := ""
	if query != "" {
		normalized, err = search.Query(query)
		if err != nil {
			return PriorityResult{}, err
		}
	}
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return PriorityResult{}, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return PriorityResult{}, err
	}
	h, err := s.repository.HiddenPaths(ctx)
	if err != nil {
		return PriorityResult{}, err
	}
	files := []TreeItem{}
	for _, root := range []string{"music", "vido"} {
		if kind == "audio" && root != "music" || kind == "video" && root != "vido" {
			continue
		}
		directory := root
		if name != "" {
			if strings.Split(name, "/")[0] != root {
				continue
			}
			directory = name
		}
		found, err := s.scan(ctx, directory, h)
		if err != nil {
			return PriorityResult{}, err
		}
		files = append(files, found...)
	}
	objects := make([]store.MediaObject, 0, len(files))
	for _, file := range files {
		kind := "audio"
		if strings.HasPrefix(file.Path, "vido/") {
			kind = "video"
		}
		objects = append(objects, store.MediaObject{Path: file.Path, Kind: kind})
	}
	ids, err := s.repository.EnsureObjects(ctx, objects)
	if err != nil {
		return PriorityResult{}, err
	}
	identifiers := make([]string, 0, len(ids))
	for _, id := range ids {
		identifiers = append(identifiers, id)
	}
	stats, err := s.repository.Stats(ctx, identifiers)
	if err != nil {
		return PriorityResult{}, err
	}
	result := PriorityResult{Items: []PriorityItem{}, Directories: []PriorityDirectory{}, Scope: name, Parent: directoryParent(name), CatalogTotal: len(files), Searching: normalized != "", Minimum: -7, Maximum: 500}
	prefix := ""
	if name != "" {
		prefix = name + "/"
	}
	counts := map[string]int{}
	for i, file := range files {
		remainder := strings.TrimPrefix(file.Path, prefix)
		direct := !strings.Contains(remainder, "/")
		if !direct {
			counts[prefix+strings.Split(remainder, "/")[0]]++
		}
		title := strings.TrimSuffix(file.Name, path.Ext(file.Name))
		if normalized != "" {
			text, err := s.search.Text(title, file.Path)
			if err != nil {
				return PriorityResult{}, err
			}
			if !strings.Contains(text, normalized) {
				continue
			}
		} else if !direct {
			continue
		}
		id := ids[file.Path]
		result.Items = append(result.Items, PriorityItem{MediaPath: file.Path, Title: title, Type: objects[i].Kind, Hidden: file.Hidden, MediaID: id, PlaybackStats: stats[id]})
	}
	for directory, count := range counts {
		result.Directories = append(result.Directories, PriorityDirectory{Name: path.Base(directory), Path: directory, Count: count})
	}
	fold := cases.Fold()
	sort.Slice(result.Directories, func(i, j int) bool {
		a, b := result.Directories[i], result.Directories[j]
		if fold.String(a.Name) != fold.String(b.Name) {
			return fold.String(a.Name) < fold.String(b.Name)
		}
		return fold.String(a.Path) < fold.String(b.Path)
	})
	sort.Slice(result.Items, func(i, j int) bool {
		a, b := result.Items[i], result.Items[j]
		if a.Preference != b.Preference {
			return a.Preference > b.Preference
		}
		if fold.String(a.MediaPath) != fold.String(b.MediaPath) {
			return fold.String(a.MediaPath) < fold.String(b.MediaPath)
		}
		return a.MediaID < b.MediaID
	})
	total := len(result.Items)
	pages := max(1, (total+pageSize-1)/pageSize)
	page = min(page, pages)
	start := (page - 1) * pageSize
	result.Items = result.Items[start:min(start+pageSize, total)]
	result.Pagination = Pagination{page, pages, total, pageSize}
	return result, nil
}
