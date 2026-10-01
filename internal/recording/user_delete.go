package recording

import (
	"context"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"os"
	"strings"
)

func (s *Storage) userFiles(ctx context.Context, relationship, user string) ([]string, error) {
	if !identifier.MatchString(relationship) || !identifier.MatchString(user) {
		return nil, store.ErrRecordingState
	}
	release, e := s.acquire(ctx, false)
	if e != nil {
		return nil, e
	}
	defer release()
	base := relationship + "/" + user
	for _, name := range []string{relationship, base} {
		info, e := s.root.Lstat(name)
		if errors.Is(e, os.ErrNotExist) {
			return nil, nil
		}
		if e != nil {
			return nil, e
		}
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return nil, ErrRecovery
		}
	}
	dir, e := s.root.Open(base)
	if e != nil {
		return nil, e
	}
	defer dir.Close()
	entries, e := dir.ReadDir(-1)
	if e != nil {
		return nil, e
	}
	ids := []string{}
	for _, entry := range entries {
		id := strings.TrimSuffix(entry.Name(), ".bin")
		if id == entry.Name() || !identifier.MatchString(id) || entry.Type()&os.ModeSymlink != 0 || entry.IsDir() {
			return nil, ErrRecovery
		}
		ids = append(ids, id)
	}
	return ids, nil
}

// Owned user cleanup acknowledges success only after both the durable ledger
// and exact private owner directory are empty. Unknown legacy bytes are fenced
// for explicit ledger adoption, never silently discarded or refunded.
func (s *Storage) DeleteUser(ctx context.Context, relationship, user string, a store.NodeAudit) (int64, error) {
	if !identifier.MatchString(user) {
		return 0, store.ErrRecordingState
	}
	n, e := s.nodes.ReadIdentity(ctx)
	if e != nil {
		return 0, e
	}
	if n.Role != "Follower" {
		return 0, store.ErrNodeState
	}
	if e = s.repo.StageOwnedUserDeletion(ctx, relationship, user); e != nil {
		return 0, e
	}
	var removed int64
	for {
		rows := []store.Recording{}
		for _, state := range []string{"pending", "ready", "deleting"} {
			items, e := s.repo.ListRecordings(ctx, user, state, 500)
			if e != nil {
				return removed, e
			}
			rows = append(rows, items...)
		}
		if len(rows) == 0 {
			break
		}
		var failures []error
		for _, v := range rows {
			unlock, e := s.Lock(v.ID)
			if e != nil {
				failures = append(failures, e)
				continue
			}
			receipt, statErr := s.Stat(ctx, relationship, user, v.ID)
			e = s.Delete(ctx, relationship, user, v.ID, store.KaraokeAudit{}, a)
			unlock()
			if e != nil {
				failures = append(failures, e)
			} else if statErr == nil {
				removed += receipt.Bytes
			}
		}
		if len(failures) != 0 {
			return removed, errors.Join(failures...)
		}
	}
	files, e := s.userFiles(ctx, relationship, user)
	if e != nil {
		return removed, e
	}
	if len(files) != 0 {
		return removed, ErrRecovery
	}
	return removed, nil
}
