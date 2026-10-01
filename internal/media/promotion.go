package media

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"path"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (s *Service) ControlLease(ctx context.Context) (func(), error) {
	if info, err := s.root.Lstat(".node-control.lock"); err == nil && !info.Mode().IsRegular() {
		return nil, ErrRecovery
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	file, err := s.root.OpenFile(".node-control.lock", os.O_RDWR|os.O_CREATE, 0600)
	if err != nil {
		return nil, err
	}
	return filelease.Acquire(ctx, file, true)
}

func (s *Service) PhysicalCapacity(ctx context.Context) (total, free int64, err error) {
	release, err := s.acquire(ctx, false)
	if err != nil {
		return 0, 0, err
	}
	defer release()
	if err = s.ready(); err != nil {
		return 0, 0, err
	}
	return fsutil.DiskUsage(s.root)
}

// WithPromotion holds the same process/volume lease as upload/rename/delete
// while checking emptiness or adopting a local placement manifest. The caller
// verifies its HTTPS identity BEFORE entering this filesystem/SQL boundary.
func (s *Service) WithPromotion(ctx context.Context, role string, apply func(store.NodePromotion) error) error {
	if role != "Master" && role != "Follower" {
		return fmt.Errorf("%w: invalid promotion role", store.ErrNodeState)
	}
	release, err := s.acquire(ctx, true)
	if err != nil {
		return err
	}
	defer release()
	if err = s.ready(); err != nil {
		return err
	}
	_, free, err := fsutil.DiskUsage(s.root)
	if err != nil {
		return err
	}
	promotion := store.NodePromotion{Role: role, PhysicalFree: free, Media: []store.LocalMedia{}}
	for _, root := range []string{"music", "vido", "lyrics"} {
		err = fs.WalkDir(s.root.FS(), root, func(name string, entry fs.DirEntry, walkErr error) error {
			if err := ctx.Err(); err != nil {
				return err
			}
			if walkErr != nil {
				return walkErr
			}
			if entry.Type()&os.ModeSymlink != 0 {
				if role == "Follower" {
					return fmt.Errorf("%w: Follower business tree contains a symlink", store.ErrNodeState)
				}
				return nil
			}
			if entry.IsDir() {
				return nil
			}
			info, err := entry.Info()
			if err != nil {
				return err
			}
			if !info.Mode().IsRegular() {
				return fmt.Errorf("%w: unsupported business filesystem object", store.ErrNodeState)
			}
			if role == "Follower" {
				if name != defaultLyric {
					return fmt.Errorf("%w: Standalone still has media or lyrics", store.ErrNodeState)
				}
				return nil
			}
			if root == "lyrics" {
				return nil
			}
			if info.Size() > math.MaxInt64-promotion.PhysicalUsed {
				return fmt.Errorf("business volume capacity overflow")
			}
			promotion.PhysicalUsed += info.Size()
			if !managedObject(name, false) || !validExt(root, name) {
				return nil
			}
			for _, part := range strings.Split(name, "/") {
				if strings.HasPrefix(part, ".") {
					return nil
				}
			}
			kind := "audio"
			if root == "vido" {
				kind = "video"
			}
			etag := fmt.Sprintf("\"%x-%x\"", info.ModTime().Unix(), info.Size())
			promotion.Media = append(promotion.Media, store.LocalMedia{MediaObject: store.MediaObject{Path: path.Clean(name), Kind: kind}, Bytes: info.Size(), ETag: etag, CreatedAt: info.ModTime().Unix(), UpdatedAt: info.ModTime().Unix()})
			return nil
		})
		if err != nil {
			return err
		}
	}
	return apply(promotion)
}
