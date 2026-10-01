//go:build !windows

package fsutil

import (
	"errors"
	"math"
	"os"

	"golang.org/x/sys/unix"
)

func DiskUsage(root *os.Root) (total, free int64, err error) {
	file, err := root.Open(".")
	if err != nil {
		return 0, 0, err
	}
	defer file.Close()
	var stat unix.Statfs_t
	if err = unix.Fstatfs(int(file.Fd()), &stat); err != nil {
		return 0, 0, err
	}
	if stat.Bsize <= 0 || uint64(stat.Blocks) > math.MaxInt64/uint64(stat.Bsize) || uint64(stat.Bavail) > math.MaxInt64/uint64(stat.Bsize) {
		return 0, 0, errors.New("filesystem capacity out of range")
	}
	return int64(stat.Blocks) * int64(stat.Bsize), int64(stat.Bavail) * int64(stat.Bsize), nil
}
