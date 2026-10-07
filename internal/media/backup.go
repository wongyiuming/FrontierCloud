package media

import (
	"context"
	"os"
)

// WithBusinessBackup pins lyric bytes and the database snapshot to the same
// media mutation boundary, across processes. No network RPC holds this lease.
func (s *Service) WithBusinessBackup(ctx context.Context, export func(*os.Root) error) error {
	// A shared lease blocks physical publication/rename/delete but lets public
	// reads continue throughout the snapshot and lyric copy.
	release, err := s.acquire(ctx, false)
	if err != nil {
		return err
	}
	defer release()
	if err := s.ready(); err != nil {
		return err
	}
	return export(s.root)
}
