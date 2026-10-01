package bootstrap

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

var mediaDirectories = []string{"media", "media/music", "media/vido", "media/lyrics", "recordings"}

// InitializeMedia creates and repairs the shared data volume without following symlinks.
func InitializeMedia(root string) error {
	if err := os.MkdirAll(root, 0o750); err != nil {
		return fmt.Errorf("create data root: %w", err)
	}
	for _, relative := range mediaDirectories {
		if err := os.MkdirAll(filepath.Join(root, filepath.FromSlash(relative)), 0o750); err != nil {
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
		if err := os.Chown(path, applicationID, applicationID); err != nil {
			return fmt.Errorf("own media path %s: %w", path, err)
		}
		return nil
	})
}
