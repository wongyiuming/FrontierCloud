package node

import (
	"context"
	"errors"
	"log/slog"
	"math"
	"sync"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func heartbeatWindow(previous map[string]any, rtt int, now int64) map[string]any {
	var samples [][2]int64
	if old, ok := previous["heartbeat"].(map[string]any); ok {
		if values, ok := old["samples"].([]any); ok {
			for _, raw := range values {
				item, ok := raw.([]any)
				if !ok || len(item) != 2 {
					continue
				}
				stamp, ok1 := intField(map[string]any{"v": item[0]}, "v")
				latency, ok2 := intField(map[string]any{"v": item[1]}, "v")
				if ok1 && ok2 && stamp >= now-3600 && stamp <= now && latency >= 0 && latency <= math.MaxInt32 {
					samples = append(samples, [2]int64{stamp, latency})
				}
			}
		}
	}
	samples = append(samples, [2]int64{now, int64(max(0, rtt))})
	if len(samples) > 180 {
		samples = samples[len(samples)-180:]
	}
	minimum, maximum, total := samples[0][1], samples[0][1], int64(0)
	for _, sample := range samples {
		minimum = min(minimum, sample[1])
		maximum = max(maximum, sample[1])
		total += sample[1]
	}
	return map[string]any{"current_ms": samples[len(samples)-1][1], "min_ms": minimum, "avg_ms": int64(math.RoundToEven(float64(total) / float64(len(samples)))), "max_ms": maximum, "count": len(samples), "window_seconds": 3600, "samples": samples}
}

func (s *Service) Revoke(ctx context.Context, id string, a store.NodeAudit) error {
	relation, err := s.repo.Relationship(ctx, id)
	if err != nil {
		return err
	}
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	apply := func() error { return s.repo.RevokeRelationship(ctx, id, false, a) }
	if row.Role == "Follower" {
		if s.volume == nil {
			return errors.New("business volume is not configured")
		}
		err = s.volume.WithPromotion(ctx, "Follower", func(store.NodePromotion) error { return s.emptyRecordings(ctx, apply) })
	} else {
		err = apply()
	}
	if err != nil {
		return err
	}
	// The durable tombstone is authoritative immediately; an unreachable peer is
	// retried by the control loop. No new pairing is allowed before its receipt.
	if err = s.NotifyRevocation(ctx, relation); err != nil {
		slog.Warn("peer revocation pending", "relationship_id", id)
	}
	return nil
}

func (s *Service) Tick(ctx context.Context, relation store.Relationship) error {
	if relation.State == "revoked" {
		return s.NotifyRevocation(ctx, relation)
	}
	if relation.State == "pending" {
		if time.Now().Unix()-relation.CreatedAt > 300 {
			return s.Revoke(ctx, relation.ID, store.NodeAudit{Actor: "pair-timeout"})
		}
		if relation.Direction != "downstream" {
			return nil
		}
		if _, err := s.Call(ctx, relation, "/internal/v1/confirm", map[string]any{}); err != nil {
			return err
		}
		if err := s.repo.ActivateRelationship(ctx, relation.ID, store.NodeAudit{Actor: "pair-recovery"}); err != nil {
			return err
		}
		var err error
		relation, err = s.repo.Relationship(ctx, relation.ID)
		if err != nil {
			return err
		}
	}
	if relation.State != "active" {
		return errors.New("invalid relationship state")
	}
	started := time.Now()
	probe := func() (map[string]any, error) {
		if relation.Status != "online" {
			role := "Master"
			if relation.Direction == "downstream" {
				role = "Follower"
			}
			if _, err := s.transport.Identity(ctx, relation.Endpoint, relation.PeerID, relation.PublicKey, role); err != nil {
				return nil, err
			}
		}
		value := map[string]any{}
		if relation.Direction == "downstream" {
			if s.pool == nil {
				return nil, errors.New("resource pool is not configured")
			}
			config, err := s.pool.MemberConfiguration(ctx, relation.PeerID)
			if err != nil {
				return nil, err
			}
			value = map[string]any{"mode": relation.Mode, "resources": resourceWire(config)}
		}
		summary, err := s.Call(ctx, relation, "/internal/v1/heartbeat", value)
		if err != nil {
			return nil, err
		}
		version, valid := intField(summary, "protocol")
		if !valid || version != protocol.Version {
			return nil, errors.New("heartbeat protocol mismatch")
		}
		return summary, nil
	}
	summary, err := probe()
	now := time.Now().Unix()
	if err != nil {
		if errors.Is(ctx.Err(), context.Canceled) {
			return err
		}
		cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Second)
		defer cancel()
		if recordErr := s.repo.RecordHeartbeat(cleanup, relation.ID, false, 0, nil, now); recordErr != nil {
			return recordErr
		}
		return err
	}
	rtt := int(min(startedElapsed(started), math.MaxInt32))
	summary["heartbeat"] = heartbeatWindow(relation.Summary, rtt, now)
	return s.repo.RecordHeartbeat(ctx, relation.ID, true, rtt, summary, now)
}
func startedElapsed(started time.Time) int64 { return max(0, time.Since(started).Milliseconds()) }

// Run is joined during shutdown. Control probes have their own small connection
// pool, and no media body ever passes through this scheduler or control client.
func (s *Service) Run(ctx context.Context) {
	if s.volume == nil {
		slog.Error("node control volume is not configured")
		return
	}
	release, err := s.volume.ControlLease(ctx)
	if err != nil {
		if ctx.Err() == nil {
			slog.Error("node control lease unavailable")
		}
		return
	}
	defer release()
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		if ctx.Err() != nil {
			return
		}
		relations, err := s.repo.Relationships(ctx, true)
		if err != nil {
			slog.Warn("node control state unavailable")
		} else {
			slots := make(chan struct{}, 4)
			var workers sync.WaitGroup
			for _, relation := range relations {
				select {
				case slots <- struct{}{}:
				case <-ctx.Done():
					workers.Wait()
					return
				}
				workers.Go(func() {
					defer func() { <-slots }()
					probe, cancel := context.WithTimeout(ctx, 15*time.Second)
					defer cancel()
					if err := s.Tick(probe, relation); err != nil && ctx.Err() == nil {
						slog.Warn("node control probe failed", "relationship_id", relation.ID)
					}
				})
			}
			workers.Wait()
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		case <-s.wakeup:
		}
	}
}
