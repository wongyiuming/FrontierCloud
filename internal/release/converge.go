package release

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// Converge is run inside the newly healthy Master, never inside the privileged
// updater. Each call is authenticated by the current persisted relationship.
func (s *Coordinator) Converge(parent context.Context, target, mode string) error {
	return s.converge(parent, target, mode, 4*time.Second)
}
func (s *Coordinator) converge(parent context.Context, target, mode string, interval time.Duration) error {
	if !ValidSHA(target) || (mode != "upgrade" && mode != "rollback") || !s.Policy.Valid() || s.Control == nil {
		return errors.New("invalid cluster release")
	}
	ctx, cancel := context.WithTimeout(parent, 15*time.Minute)
	defer cancel()
	identity, err := s.Nodes.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	if identity.Role != "Master" {
		return store.ErrNodeState
	}
	all, err := s.Nodes.Relationships(ctx, false)
	if err != nil {
		return err
	}
	selected := map[string]store.Relationship{}
	for _, r := range all {
		if r.Direction == "downstream" && r.State == "active" {
			selected[r.ID] = r
		}
	}
	if len(selected) > 1000 {
		return errors.New("too many release followers")
	}
	check := func() error {
		current, err := s.Nodes.ReadIdentity(ctx)
		if err != nil {
			return err
		}
		if current.ID != identity.ID || current.Role != "Master" {
			return store.ErrNodeState
		}
		rows, err := s.Nodes.Relationships(ctx, false)
		if err != nil {
			return err
		}
		count := 0
		for _, r := range rows {
			if r.Direction != "downstream" || r.State != "active" {
				continue
			}
			count++
			old, ok := selected[r.ID]
			if !ok || !sameReleasePeer(old, r) {
				return store.ErrNodeState
			}
		}
		if count != len(selected) {
			return store.ErrNodeState
		}
		return nil
	}
	if err = check(); err != nil {
		return err
	}
	if len(selected) == 0 {
		return check()
	}
	if err = s.parallel(ctx, selected, func(ctx context.Context, r store.Relationship) error {
		if e := check(); e != nil {
			return e
		}
		current, e := s.Nodes.Relationship(ctx, r.ID)
		if e != nil || !sameReleasePeer(r, current) {
			return store.ErrNodeState
		}
		out, e := s.Control.Call(ctx, current, "/internal/v1/cluster-update/start", map[string]any{"target_sha": target, "mode": mode})
		if e != nil || out["accepted"] != true {
			return errors.New("Follower release request not acknowledged")
		}
		return nil
	}); err != nil {
		return err
	}
	for {
		if err = check(); err != nil {
			return err
		}
		statuses, err := s.followers(ctx, identity)
		if err != nil {
			return err
		}
		complete := true
		for _, f := range statuses {
			status, _ := f["status"].(map[string]any)
			if f["reachable"] == true && status["target_sha"] == target && status["state"] == "failed" {
				return errors.New("Follower release failed; local generation retained")
			}
			if f["reachable"] != true || status["release_branch"] != s.Policy.Branch || status["current_sha"] != target || status["state"] != "success" {
				complete = false
			}
		}
		if complete {
			return check()
		}
		select {
		case <-ctx.Done():
			return errors.New("cluster release convergence deadline reached")
		case <-time.After(interval):
		}
	}
}
func sameReleasePeer(a, b store.Relationship) bool {
	return a.ID == b.ID && a.State == "active" && b.State == "active" && a.Direction == "downstream" && b.Direction == "downstream" && a.PeerID == b.PeerID && a.Endpoint == b.Endpoint && a.PublicKey == b.PublicKey && a.Protocol == b.Protocol
}
func (s *Coordinator) parallel(ctx context.Context, selected map[string]store.Relationship, call func(context.Context, store.Relationship) error) error {
	slots := make(chan struct{}, 4)
	var wg sync.WaitGroup
	var mu sync.Mutex
	var failure error
	for _, r := range selected {
		select {
		case slots <- struct{}{}:
		case <-ctx.Done():
			wg.Wait()
			return ctx.Err()
		}
		wg.Go(func() {
			defer func() { <-slots }()
			probe, cancel := context.WithTimeout(ctx, 10*time.Second)
			defer cancel()
			if err := call(probe, r); err != nil {
				mu.Lock()
				failure = err
				mu.Unlock()
			}
		})
	}
	wg.Wait()
	return failure
}
