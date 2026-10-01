package media

import (
	"context"
	"path"
	"sort"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/store"
	"golang.org/x/text/cases"
)

func globalTreeItem(row store.GlobalMedia, h map[string]bool) TreeItem {
	size := row.Bytes
	return TreeItem{Name: path.Base(row.Path), Path: row.Path, Kind: "file", Size: &size, Hidden: hidden(row.Path, h), HiddenDirect: h[row.Path], Media: true, MediaID: row.ID, MemberID: row.MemberID, Transport: row.Transport, NodeHealth: row.Health}
}
func (s *Service) globalScan(ctx context.Context, scope string, h map[string]bool) ([]TreeItem, error) {
	rows, err := s.pool.Resources(ctx, scope, false)
	if err != nil {
		return nil, err
	}
	items := make([]TreeItem, 0, len(rows))
	for _, row := range rows {
		items = append(items, globalTreeItem(row, h))
	}
	return items, nil
}
func (s *Service) globalTree(ctx context.Context, scope string) (Tree, error) {
	h, err := s.repository.HiddenPaths(ctx)
	if err != nil {
		return Tree{}, err
	}
	files, err := s.globalScan(ctx, scope, h)
	if err != nil {
		return Tree{}, err
	}
	found := map[string]TreeItem{}
	prefix := scope + "/"
	for _, file := range files {
		if !strings.HasPrefix(file.Path, prefix) {
			continue
		}
		remainder := strings.TrimPrefix(file.Path, prefix)
		if strings.Contains(remainder, "/") {
			name := strings.Split(remainder, "/")[0]
			p := prefix + name
			found[p] = TreeItem{Name: name, Path: p, Kind: "directory", Hidden: hidden(p, h), HiddenDirect: h[p], Hideable: true}
		} else {
			found[file.Path] = file
		}
	}
	result := Tree{Path: scope, Items: []TreeItem{}}
	for _, item := range found {
		result.Items = append(result.Items, item)
	}
	fold := cases.Fold()
	sort.Slice(result.Items, func(i, j int) bool {
		a, b := result.Items[i], result.Items[j]
		if a.Kind != b.Kind {
			return a.Kind == "directory"
		}
		fa, fb := fold.String(a.Name), fold.String(b.Name)
		if fa == fb {
			return a.Name < b.Name
		}
		return fa < fb
	})
	return result, nil
}
