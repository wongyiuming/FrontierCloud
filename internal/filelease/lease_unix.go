//go:build !windows

package filelease

import (
	"errors"
	"golang.org/x/sys/unix"
	"os"
)

func try(file *os.File, exclusive bool) error {
	mode := unix.LOCK_SH
	if exclusive {
		mode = unix.LOCK_EX
	}
	err := unix.Flock(int(file.Fd()), mode|unix.LOCK_NB)
	if errors.Is(err, unix.EAGAIN) || errors.Is(err, unix.EWOULDBLOCK) {
		return ErrBusy
	}
	return err
}
func unlock(file *os.File) error { return unix.Flock(int(file.Fd()), unix.LOCK_UN) }
