package bootstrap

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

var mediaDirectories = []string{"media", "media/music", "media/vido", "media/lyrics", "recordings"}

// InitializeMedia creates and repairs the shared data volume without following symlinks.
func InitializeMedia(root string) error {
	root, err := filepath.Abs(root)
	if err != nil {
		return err
	}
	if err := mkdirNoSymlinks(root, 0o755); err != nil {
		return fmt.Errorf("create data root: %w", err)
	}
	for _, relative := range mediaDirectories {
		mode := os.FileMode(0o755)
		if relative == "recordings" {
			mode = 0o750
		}
		if err := mkdirNoSymlinks(filepath.Join(root, filepath.FromSlash(relative)), mode); err != nil {
			return fmt.Errorf("create media directory: %w", err)
		}
	}
	return filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.Type()&os.ModeSymlink != 0 {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		// Nginx's worker has a different UID. Public media directories need
		// traversal rights; secrets, recordings and private operation staging
		// retain their existing modes and are never exposed by this repair.
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		parts := strings.Split(filepath.ToSlash(relative), "/")
		public := relative == "." || parts[0] == "media"
		for _, part := range parts {
			if strings.HasPrefix(part, ".") && part != "." {
				public = false
			}
		}
		if public && entry.IsDir() {
			info, err := entry.Info()
			if err != nil {
				return err
			}
			if err := os.Chmod(path, info.Mode().Perm()|0o055); err != nil {
				return err
			}
		}
		if err := os.Chown(path, applicationID, applicationID); err != nil {
			return fmt.Errorf("own media path %s: %w", path, err)
		}
		return nil
	})
}

// Validate each existing ancestor before creating the next component. In
// particular do not let MkdirAll traverse media/music -> an outside volume.
func mkdirNoSymlinks(absolute string, mode os.FileMode) error {
	current := filepath.VolumeName(absolute) + string(filepath.Separator)
	remainder := strings.TrimPrefix(absolute, current)
	for _, part := range strings.Split(remainder, string(filepath.Separator)) {
		if part == "" {
			continue
		}
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if os.IsNotExist(err) {
			if err := os.Mkdir(current, mode); err != nil && !os.IsExist(err) {
				return err
			}
			info, err = os.Lstat(current)
		}
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return fmt.Errorf("unsafe data directory: %s", current)
		}
	}
	return nil
}
