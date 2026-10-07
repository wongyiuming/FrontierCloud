//go:build linux

package media

import "golang.org/x/sys/unix"

// Do not overwrite even an empty destination created by a concurrent external
// filesystem writer. Both directory locators are anchored to the media root.
func (s *Service) renameExclusive(old, target string) error {
	f, err := s.root.Open(".")
	if err != nil {
		return err
	}
	defer f.Close()
	return unix.Renameat2(int(f.Fd()), old, int(f.Fd()), target, unix.RENAME_NOREPLACE)
}
