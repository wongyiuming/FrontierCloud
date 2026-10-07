//go:build !windows

package fsutil

import (
	"os"
	"syscall"
)

// Root-invoked operator commands must not leave 0600 gate files unreadable to
// the application's volume owner. Change only the exact opened gate inode.
func InheritOwner(file *os.File, root *os.Root) error {
	parent, err := root.Stat(".")
	if err != nil {
		return err
	}
	owner := parent.Sys().(*syscall.Stat_t)
	current, err := file.Stat()
	if err != nil {
		return err
	}
	actual := current.Sys().(*syscall.Stat_t)
	if owner.Uid == actual.Uid && owner.Gid == actual.Gid {
		return nil
	}
	return file.Chown(int(owner.Uid), int(owner.Gid))
}
