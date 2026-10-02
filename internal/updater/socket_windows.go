//go:build windows

package updater

import "os"

func directoryPermissions(name string) error { return os.Chmod(name, 0700) }

func socketPermissions(name string) error { return os.Chmod(name, 0600) }
