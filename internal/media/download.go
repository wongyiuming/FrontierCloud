package media

import (
	"archive/zip"
	"context"
	"errors"
	"io"
	"os"
	"path"
	"strings"
	"sync"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// Download owns a shared mutation lease. Archive bytes are emitted directly to
// the HTTP writer; no temporary archive or payload-sized buffer is created.
type Download struct {
	service *Service
	ctx     context.Context
	Items   []store.DeleteItem
	release sync.Once
	lease   func()
}

func (d *Download) Close() { d.release.Do(d.lease) }

func (s *Service) Download(ctx context.Context, paths []string) (*Download, error) {
	release, err := s.acquire(ctx, false)
	if err != nil {
		return nil, err
	}
	value := &Download{service: s, ctx: ctx, Items: []store.DeleteItem{}, lease: release}
	fail := func(err error) (*Download, error) { value.Close(); return nil, err }
	if err := s.ready(); err != nil {
		return fail(err)
	}
	if len(paths) == 0 {
		return fail(ErrPath)
	}
	seen := map[string]bool{}
	for _, name := range paths {
		if err := ctx.Err(); err != nil {
			return fail(err)
		}
		name = strings.TrimSpace(strings.ReplaceAll(name, "\\", "/"))
		info, err := s.safeInfo(name)
		if err != nil {
			return fail(err)
		}
		if !managedObject(name, info.IsDir()) || (!info.IsDir() && !info.Mode().IsRegular()) {
			return fail(ErrPath)
		}
		if !seen[name] {
			seen[name] = true
			value.Items = append(value.Items, store.DeleteItem{Path: name, Directory: info.IsDir()})
		}
	}
	return value, nil
}

func (d *Download) Open(name string) (*os.File, os.FileInfo, error) {
	if err := d.ctx.Err(); err != nil {
		return nil, nil, err
	}
	info, err := d.service.safeInfo(name)
	if err != nil {
		return nil, nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, nil, ErrPath
	}
	f, err := d.service.root.Open(name)
	if err != nil {
		return nil, nil, err
	}
	// Legacy uploads may be 0600. Match the old Admin download repair for the
	// independent unprivileged Nginx container; never widen write permissions.
	if info.Mode().Perm()&0044 != 0044 {
		if err := f.Chmod(info.Mode().Perm() | 0044); err != nil {
			f.Close()
			return nil, nil, err
		}
	}
	return f, info, nil
}

func (d *Download) ZIP(writer io.Writer) error {
	z := zip.NewWriter(writer)
	seen := map[string]bool{}
	buffer := make([]byte, 64*1024)
	add := func(name string) error {
		if seen[name] {
			return nil
		}
		seen[name] = true
		f, info, err := d.Open(name)
		if err != nil {
			return err
		}
		defer f.Close()
		header, err := zip.FileInfoHeader(info)
		if err != nil {
			return err
		}
		header.Name = name
		header.Method = zip.Store
		member, err := z.CreateHeader(header)
		if err != nil {
			return err
		}
		_, err = io.CopyBuffer(member, &contextReader{d.ctx, f}, buffer)
		return err
	}
	var visit func(string) error
	visit = func(dir string) error {
		if err := d.ctx.Err(); err != nil {
			return err
		}
		info, err := d.service.safeInfo(dir)
		if err != nil || !info.IsDir() {
			return ErrPath
		}
		f, err := d.service.root.Open(dir)
		if err != nil {
			return err
		}
		defer f.Close()
		for {
			entries, readErr := f.ReadDir(128)
			for _, entry := range entries {
				if err := d.ctx.Err(); err != nil {
					return err
				}
				if entry.Type()&os.ModeSymlink != 0 || strings.HasPrefix(entry.Name(), ".") {
					continue
				}
				name := dir + "/" + entry.Name()
				if entry.IsDir() {
					if len(strings.Split(name, "/")) <= 3 {
						if err := visit(name); err != nil {
							return err
						}
					}
					continue
				}
				if entry.Type().IsRegular() && managedObject(name, false) {
					if err := add(name); err != nil {
						return err
					}
				}
			}
			if errors.Is(readErr, io.EOF) {
				break
			}
			if readErr != nil {
				return readErr
			}
		}
		return nil
	}
	for _, item := range d.Items {
		if item.Directory {
			if err := visit(item.Path); err != nil {
				return err
			}
		} else if err := add(item.Path); err != nil {
			return err
		}
	}
	return z.Close()
}

type contextReader struct {
	ctx    context.Context
	reader io.Reader
}

func (r *contextReader) Read(buffer []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.reader.Read(buffer)
}
func DownloadFilename(name string) string { return path.Base(name) }
