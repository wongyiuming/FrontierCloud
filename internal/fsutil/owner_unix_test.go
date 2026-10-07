//go:build !windows

package fsutil

import (
	"os"
	"syscall"
	"testing"
)

func TestRootGateFilesInheritVolumeOwner(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("requires root operator fixture")
	}
	directory := t.TempDir()
	if err := os.Chown(directory, 10001, 10001); err != nil {
		t.Fatal(err)
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	file, err := root.OpenFile("gate", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if err := InheritOwner(file, root); err != nil {
		t.Fatal(err)
	}
	info, err := file.Stat()
	if err != nil {
		t.Fatal(err)
	}
	owner := info.Sys().(*syscall.Stat_t)
	if owner.Uid != 10001 || owner.Gid != 10001 || info.Mode().Perm() != 0600 {
		t.Fatal("root command left a private gate inaccessible to the volume owner", info)
	}
}
