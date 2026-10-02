//go:build !windows

package updater

import "os"

func directoryPermissions(name string) error {
	if os.Geteuid() == 0 {
		if err := os.Chown(name, 0, 10001); err != nil {
			return err
		}
	}
	return os.Chmod(name, 0750)
}

func socketPermissions(name string) error {
	if os.Geteuid() == 0 {
		if err := os.Chown(name, 0, 10001); err != nil {
			return err
		}
	}
	return os.Chmod(name, 0660)
}
