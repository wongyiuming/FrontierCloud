//go:build !linux

package media

import (
	"errors"
	"os"
)

func (s *Service) renameExclusive(old, target string) error {
	if _, err := s.root.Lstat(target); err == nil {
		return os.ErrExist
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return s.root.Rename(old, target)
}
