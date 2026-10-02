package media

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"io"
	"io/fs"
	"os"
	"path"
	"strings"
)

// AdoptOwnedStorage must run under the native OFFLINE lifecycle lease. It only
// adds exact-size SQL publication receipts for already registered Follower
// objects. It neither discovers new IDs nor recovers any physical intent.
// The operator still owns isolation of legacy/remote writers not using Go leases.
func AdoptOwnedStorage(ctx context.Context, directory string, repo store.MaintenanceRepository, mediaRepo store.MediaRepository, nodes store.NodeRepository, relationship, confirmation string) (int, error) {
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
	if err := s.ready(); err != nil {
		return 0, err
	}
	n, err := nodes.ReadIdentity(ctx)
	if err != nil {
		return 0, err
	}
	if n.Role != "Follower" || n.ID != confirmation {
		return 0, store.ErrNodeState
	}
	objects, err := repo.OwnedAdoptionObjects(ctx, relationship)
	if err != nil {
		return 0, err
	}
	byPath := map[string]store.MediaObject{}
	for _, o := range objects {
		if !ownedObjectID.MatchString(o.ID) || !managedObject(o.Path, false) || !validExt(strings.Split(o.Path, "/")[0], o.Path) || strings.HasPrefix(o.Path, "lyrics/") {
			return 0, ErrPath
		}
		if _, duplicate := byPath[o.Path]; duplicate {
			return 0, store.ErrNodeState
		}
		byPath[o.Path] = o
	}
	var proofs []store.LocalMedia
	for _, name := range []string{"music", "vido"} {
		err = fs.WalkDir(root.FS(), name, func(name string, entry fs.DirEntry, walkErr error) error {
			if err := ctx.Err(); err != nil {
				return err
			}
			if walkErr != nil {
				return walkErr
			}
			if entry.Type()&os.ModeSymlink != 0 {
				return ErrPath
			}
			if entry.IsDir() {
				return nil
			}
			object, ok := byPath[name]
			if !ok {
				return errors.Join(ErrRecovery, errors.New("unregistered physical object prevents adoption"))
			}
			before, err := s.safeInfo(name)
			if err != nil {
				return err
			}
			if !before.Mode().IsRegular() || before.Size() <= 0 || before.Size() > 10*store.GiB {
				return ErrPath
			}
			file, err := root.Open(name)
			if err != nil {
				return err
			}
			opened, err := file.Stat()
			if err != nil || !os.SameFile(before, opened) {
				file.Close()
				return ErrRecovery
			}
			digest := sha256.New()
			bytes, copyErr := io.CopyBuffer(digest, &contextReader{ctx, io.LimitReader(file, before.Size()+1)}, make([]byte, 64*1024))
			after, statErr := file.Stat()
			closeErr := file.Close()
			current, pathErr := root.Lstat(name)
			if err := errors.Join(copyErr, statErr, closeErr, pathErr); err != nil {
				return err
			}
			if bytes != before.Size() || after.Size() != before.Size() || !before.ModTime().Equal(after.ModTime()) || !os.SameFile(before, current) || !current.ModTime().Equal(before.ModTime()) {
				return ErrRecovery
			}
			proofs = append(proofs, store.LocalMedia{MediaObject: object, Bytes: bytes, ETag: `"` + hex.EncodeToString(digest.Sum(nil)) + `"`})
			delete(byPath, name)
			return nil
		})
		if err != nil {
			return 0, err
		}
	}
	if len(byPath) != 0 {
		return 0, ErrRecovery
	}
	// Any private in-progress native/legacy staging file means that quiescence
	// is insufficient physical proof. Never remove it or manufacture a refund.
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
		base := path.Base(name)
		if strings.HasPrefix(base, ".") && name != "." {
			if name == ".media-mutation.lock" || name == ".node-control.lock" {
				info, err := entry.Info()
				if err != nil {
					return err
				}
				if !info.Mode().IsRegular() {
					return ErrRecovery
				}
				return nil
			}
			return ErrRecovery
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return repo.AdoptOwnedStorage(ctx, relationship, confirmation, proofs, store.NodeAudit{Actor: "offline-owned-adoption"})
}
