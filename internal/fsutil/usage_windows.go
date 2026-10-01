package fsutil

import (
	"errors"
	"math"
	"os"

	"golang.org/x/sys/windows"
)

func DiskUsage(root *os.Root) (total, free int64, err error) {
	name, err := windows.UTF16PtrFromString(root.Name())
	if err != nil {
		return 0, 0, err
	}
	var all, available uint64
	if err = windows.GetDiskFreeSpaceEx(name, &available, &all, nil); err != nil {
		return 0, 0, err
	}
	if all > math.MaxInt64 || available > math.MaxInt64 {
		return 0, 0, errors.New("filesystem capacity out of range")
	}
	return int64(all), int64(available), nil
}
