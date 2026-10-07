//go:build windows

package fsutil

import "os"

// Windows uses inherited ACLs, not POSIX uid/gid ownership.
func InheritOwner(*os.File, *os.Root) error { return nil }
