//go:build !windows

package security

import "os"

func syncEdgeDirectory(root *os.Root) error {
	f, err := root.Open(".ip-security")
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}
