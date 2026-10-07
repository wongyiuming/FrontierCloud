package media

import (
	"context"
	"os"
	"path"
	"sort"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/store"
	"golang.org/x/text/cases"
)

type TreeItem struct {
	Name         string `json:"name"`
	Path         string `json:"path"`
	Kind         string `json:"kind"`
	Size         *int64 `json:"size"`
	Hidden       bool   `json:"hidden"`
	HiddenDirect bool   `json:"hidden_direct"`
	Media        bool   `json:"media"`
	Hideable     bool   `json:"hideable"`
	MediaID      string `json:"media_id,omitempty"`
	MemberID     string `json:"storage_member_id,omitempty"`
	Transport    string `json:"transport,omitempty"`
	NodeHealth   string `json:"node_health,omitempty"`
}
type Tree struct {
	Path  string     `json:"path"`
	Items []TreeItem `json:"items"`
}
type DirectoryPreference struct {
	Path       string `json:"path"`
	Preference int    `json:"preference"`
}
type PreferenceResult struct {
	store.PlaybackResult
	Path string
}

func normalizeAdminPath(value string, allowEmpty bool) (string, error) {
	value = strings.TrimSpace(strings.ReplaceAll(value, "\\", "/"))
	if value == "" && allowEmpty {
		return "", nil
	}
	if value == "" || strings.HasPrefix(value, "/") || strings.ContainsRune(value, 0) || (len(value) >= 2 && value[1] == ':') {
		return "", ErrPath
	}
	parts := []string{}
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." {
			continue
		}
		if strings.HasPrefix(part, ".") {
			return "", ErrPath
		}
		parts = append(parts, part)
	}
	if len(parts) == 0 || len(parts) > 3 || (parts[0] != "music" && parts[0] != "vido" && parts[0] != "lyrics") {
		return "", ErrPath
	}
	return strings.Join(parts, "/"), nil
}

func (s *Service) Tree(ctx context.Context, name string) (Tree, error) {
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return Tree{}, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return Tree{}, err
	}
	name, err := normalizeAdminPath(name, true)
	if err != nil {
		return Tree{}, err
	}
	role, err := s.role(ctx)
	if err != nil {
		return Tree{}, err
	}
	if role == "Master" && name != "" && !strings.HasPrefix(name, "lyrics") {
		return s.globalTree(ctx, name)
	}
	var entries []os.DirEntry
	if name == "" {
		f, err := s.root.Open(".")
		if err != nil {
			return Tree{}, err
		}
		entries, err = f.ReadDir(-1)
		f.Close()
		if err != nil {
			return Tree{}, err
		}
	} else {
		entries, err = s.readDir(name)
		if err != nil {
			return Tree{}, err
		}
	}
	h, err := s.repository.HiddenPaths(ctx)
	if err != nil {
		return Tree{}, err
	}
	result := Tree{Path: name, Items: []TreeItem{}}
	for _, entry := range entries {
		if entry.Type()&os.ModeSymlink != 0 || strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		rel := entry.Name()
		if name != "" {
			rel = name + "/" + rel
		}
		parts := strings.Split(rel, "/")
		if name == "" && (!entry.IsDir() || (rel != "music" && rel != "vido" && rel != "lyrics")) {
			continue
		}
		if entry.IsDir() && len(parts) > 3 {
			continue
		}
		if parts[0] == "lyrics" && !entry.IsDir() && (!validExt("lyrics", rel) || rel == defaultLyric) {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			return Tree{}, err
		}
		if !info.IsDir() && !info.Mode().IsRegular() {
			continue
		}
		kind := "file"
		var size *int64
		if info.IsDir() {
			kind = "directory"
		} else {
			bytes := info.Size()
			size = &bytes
		}
		media := validExt("music", rel) || validExt("vido", rel)
		result.Items = append(result.Items, TreeItem{Name: entry.Name(), Path: rel, Kind: kind, Size: size, Hidden: hidden(rel, h), HiddenDirect: h[rel], Media: media, Hideable: entry.IsDir() && parts[0] != "lyrics"})
	}
	fold := cases.Fold()
	sort.SliceStable(result.Items, func(i, j int) bool {
		a, b := result.Items[i], result.Items[j]
		if a.Kind != b.Kind {
			return a.Kind == "directory"
		}
		return fold.String(a.Name) < fold.String(b.Name)
	})
	return result, nil
}

func (s *Service) DirectoryPreferences(ctx context.Context, scope string) ([]DirectoryPreference, error) {
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return nil, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return nil, err
	}
	name, err := normalizeAdminPath(scope, true)
	if err != nil {
		return nil, err
	}
	if name != "" && strings.Split(name, "/")[0] == "lyrics" {
		return nil, ErrPath
	}
	all, err := s.repository.DirectoryPreferences(ctx)
	if err != nil {
		return nil, err
	}
	prefix := ""
	depth := 1
	if name != "" {
		prefix = name + "/"
		depth = len(strings.Split(name, "/")) + 1
	}
	items := []DirectoryPreference{}
	for path, value := range all {
		if strings.HasPrefix(path, prefix) && len(strings.Split(path, "/")) == depth {
			items = append(items, DirectoryPreference{path, value})
		}
	}
	fold := cases.Fold()
	sort.Slice(items, func(i, j int) bool {
		if items[i].Preference != items[j].Preference {
			return items[i].Preference > items[j].Preference
		}
		return fold.String(items[i].Path) < fold.String(items[j].Path)
	})
	return items, nil
}

func (s *Service) Preference(ctx context.Context, name string, value int, directory bool, audit store.AdminAudit) (PreferenceResult, error) {
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return PreferenceResult{}, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return PreferenceResult{}, err
	}
	role, err := s.role(ctx)
	if err != nil {
		return PreferenceResult{}, err
	}
	if role == "Master" && !directory {
		row, _, err := s.resolveGlobal(ctx, "", name)
		if err != nil {
			return PreferenceResult{}, err
		}
		result, err := s.pool.SetGlobalPreference(ctx, row.ID, value, audit)
		return PreferenceResult{result, row.Path}, err
	}
	var object store.MediaObject
	if directory {
		name, err = normalizeAdminPath(name, false)
		if err != nil {
			return PreferenceResult{}, err
		}
		parts := strings.Split(name, "/")
		if len(parts) < 2 || parts[0] == "lyrics" {
			return PreferenceResult{}, ErrPath
		}
		if role == "Master" {
			rows, err := s.pool.Resources(ctx, name, false)
			if err != nil {
				return PreferenceResult{}, err
			}
			if len(rows) == 0 {
				return PreferenceResult{}, os.ErrNotExist
			}
		} else {
			info, err := s.safeInfo(name)
			if err != nil {
				return PreferenceResult{}, err
			}
			if !info.IsDir() {
				return PreferenceResult{}, ErrPath
			}
		}
		object = store.MediaObject{Path: name, Kind: "directory"}
	} else {
		object, err = s.ValidateTrack(name)
		if err != nil {
			return PreferenceResult{}, err
		}
	}
	result, err := s.repository.SetPreference(ctx, object, value, audit)
	return PreferenceResult{result, object.Path}, err
}

func (s *Service) Hide(ctx context.Context, paths []string, hide bool, audit store.AdminAudit) error {
	release, leaseErr := s.acquire(ctx, true)
	if leaseErr != nil {
		return leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return err
	}
	role, err := s.role(ctx)
	if err != nil {
		return err
	}
	normalized := []string{}
	seen := map[string]bool{}
	for _, name := range paths {
		name, err := normalizeAdminPath(name, false)
		if err != nil {
			return err
		}
		parts := strings.Split(name, "/")
		if parts[0] == "lyrics" {
			return ErrPath
		}
		if role == "Master" {
			rows, err := s.pool.Resources(ctx, name, false)
			if err != nil {
				return err
			}
			if len(rows) == 0 {
				return os.ErrNotExist
			}
		} else {
			info, err := s.safeInfo(name)
			if err != nil {
				return err
			}
			if !info.IsDir() {
				return ErrPath
			}
		}
		if !seen[name] {
			normalized = append(normalized, name)
			seen[name] = true
		}
	}
	sort.Strings(normalized)
	return s.repository.SetHidden(ctx, normalized, hide, audit)
}

func directoryParent(name string) string {
	parent := path.Dir(name)
	if parent == "." {
		return ""
	}
	return parent
}
