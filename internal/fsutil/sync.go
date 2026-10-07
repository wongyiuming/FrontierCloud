package fsutil

import (
	"os"
	"runtime"
)

func SyncDirectory(root *os.Root, name string) error {
	if runtime.GOOS == "windows" {
		return nil
	} // Windows does not support fsync on directories.
	file, e := root.Open(name)
	if e != nil {
		return e
	}
	defer file.Close()
	return file.Sync()
}
