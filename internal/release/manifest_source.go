package release

import (
	"context"
	"io"
	"os"
	"path/filepath"
)

// ManifestSource is controlled deployment configuration, never an HTTP request
// target. A file is only publication metadata: exact reviewed CI proof and
// production ancestry must still independently authorize every artifact.
type ManifestSource interface {
	Latest(context.Context) (Manifest, error)
}

type FileManifestSource struct{ Path string }

func (s FileManifestSource) Latest(ctx context.Context) (Manifest, error) {
	if ctx.Err() != nil || !filepath.IsAbs(s.Path) {
		return Manifest{}, ErrManifest
	}
	root, err := os.OpenRoot(filepath.Dir(s.Path))
	if err != nil {
		return Manifest{}, ErrManifest
	}
	defer root.Close()
	name := filepath.Base(s.Path)
	info, err := root.Lstat(name)
	if err != nil || !info.Mode().IsRegular() || info.Size() > MaxManifestBytes || info.Size() == 0 {
		return Manifest{}, ErrManifest
	}
	file, err := root.Open(name)
	if err != nil {
		return Manifest{}, ErrManifest
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return Manifest{}, ErrManifest
	}
	raw, err := io.ReadAll(io.LimitReader(file, MaxManifestBytes+1))
	if err != nil || ctx.Err() != nil {
		return Manifest{}, ErrManifest
	}
	after, err := file.Stat()
	if err != nil || after.Size() != opened.Size() || !after.ModTime().Equal(opened.ModTime()) {
		return Manifest{}, ErrManifest
	}
	return ParseManifest(raw)
}
