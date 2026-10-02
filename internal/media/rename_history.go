package media

import (
	"context"
	"io/fs"
	"os"
	"path"
	"regexp"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

var offlineLeaseName = regexp.MustCompile(`^\.(session|global-rename)-[a-f0-9]{32}\.lease$`)

// DrainRenameHistory does not initialize or recover the volume. Stages, markers
// and malformed private entries fail closed; no filesystem object is removed.
func DrainRenameHistory(ctx context.Context, directory string, repo store.MaintenanceRepository, mediaRepo store.MediaRepository, confirmation string) (int, error) {
	info, err := os.Lstat(directory)
	if err != nil {
		return 0, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return 0, ErrPath
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		return 0, err
	}
	defer root.Close()
	opened, err := root.Stat(".")
	if err != nil || !os.SameFile(info, opened) {
		return 0, ErrPath
	}
	s := &Service{root: root, repository: mediaRepo}
	release, err := s.acquire(ctx, true)
	if err != nil {
		return 0, err
	}
	defer release()
	if err = s.ready(); err != nil {
		return 0, err
	}
	err = fs.WalkDir(root.FS(), ".", func(name string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return ErrPath
		}
		if name == "." {
			return nil
		}
		if !strings.HasPrefix(path.Base(name), ".") {
			return nil
		}
		if name != ".media-mutation.lock" && name != ".node-control.lock" && !offlineLeaseName.MatchString(name) {
			return ErrRecovery
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() || info.Size() != 0 {
			return ErrRecovery
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return repo.DrainCompletedRenames(ctx, confirmation, store.NodeAudit{Actor: "offline-rename-drain"})
}
