package node

import (
	"context"
	"encoding/base64"
	"errors"
	"io"
	"log/slog"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/backup"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

const backupInterval = 24 * time.Hour
const backupRetry = 5 * time.Minute

// ConfigureBackups is startup-only; background workers are joined before the
// builder, its private cache root or the authoritative repositories are closed.
func (s *Service) ConfigureBackups(repository store.BackupRepository, builder *backup.Builder) {
	s.backups, s.backupBuilder = repository, builder
}

func (s *Service) backupRelation(ctx context.Context, id string, pinned *store.Relationship) (store.Relationship, error) {
	node, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return store.Relationship{}, err
	}
	if node.Role != "Master" || s.pool == nil {
		return store.Relationship{}, store.ErrNodeState
	}
	rel, err := s.repo.Relationship(ctx, id)
	if err != nil {
		return rel, err
	}
	if rel.State != "active" || rel.Direction != "downstream" || rel.Protocol != 2 || rel.Status == "offline" || time.Now().Unix()-rel.LastHeartbeat >= 120 {
		return rel, store.ErrNodeState
	}
	if pinned != nil && (rel.PeerID != pinned.PeerID || rel.Endpoint != pinned.Endpoint || rel.PublicKey != pinned.PublicKey || rel.Credential != pinned.Credential) {
		return rel, store.ErrNodeState
	}
	config, err := s.pool.MemberConfiguration(ctx, rel.PeerID)
	if err != nil {
		return rel, err
	}
	if !config.Backup.Enabled {
		return rel, store.ErrNodeState
	}
	return rel, nil
}

// DeliverBackup is a bounded manual delivery entry point. It shares the OS
// scheduler lease with background workers in every runtime process.
func (s *Service) DeliverBackup(ctx context.Context, relationship string) error {
	if s.backupBuilder == nil || s.backups == nil {
		return store.ErrBackupState
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Minute)
	defer cancel()
	release, err := s.backupBuilder.SchedulerLease(ctx)
	if err != nil {
		return err
	}
	defer release()
	return s.deliverBackup(ctx, relationship)
}

func (s *Service) deliverBackup(ctx context.Context, relationship string) (resultErr error) {
	rel, err := s.backupRelation(ctx, relationship, nil)
	if err != nil {
		return err
	}
	artifact, err := s.backupBuilder.Build(ctx)
	if err != nil {
		return err
	}
	defer func() { resultErr = errors.Join(resultErr, artifact.Close()) }()
	call := func(ctx context.Context, operation string, value map[string]any) (map[string]any, error) {
		current, err := s.backupRelation(ctx, relationship, &rel)
		if err != nil {
			return nil, err
		}
		return s.Call(ctx, current, "/internal/v1/backup/"+operation, value)
	}
	// Older peers may not implement abort. No runtime-name branch is needed.
	_, _ = call(ctx, "abort", map[string]any{"generation": artifact.Generation})
	started := false
	defer func() {
		if resultErr != nil && started {
			cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
			defer cancel()
			_, _ = call(cleanup, "abort", map[string]any{"generation": artifact.Generation})
		}
	}()
	started = true // Even a lost begin acknowledgement can leave receiving data.
	value, err := call(ctx, "begin", map[string]any{"generation": artifact.Generation})
	if err != nil {
		return err
	}
	if textField(value, "status") != "receiving" {
		return store.ErrBackupState
	}
	buffer := make([]byte, store.MaxBackupChunk)
	for index := 0; ; index++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		n, readErr := io.ReadFull(artifact, buffer)
		if readErr == io.EOF {
			break
		}
		if readErr != nil && readErr != io.ErrUnexpectedEOF {
			return readErr
		}
		if index > store.MaxBackupChunkIndex {
			return store.ErrBackupState
		}
		value, err = call(ctx, "chunk", map[string]any{"generation": artifact.Generation, "chunk_index": index, "chunk": base64.StdEncoding.EncodeToString(buffer[:n])})
		if err != nil {
			return err
		}
		size, ok := intField(value, "bytes")
		if !ok || size != int64(n) || textField(value, "status") != "receiving" {
			return store.ErrBackupState
		}
	}
	value, err = call(ctx, "commit", map[string]any{"generation": artifact.Generation, "checksum": artifact.Checksum})
	if err != nil {
		return err
	}
	size, ok := intField(value, "bytes")
	if !ok || size != artifact.Bytes || textField(value, "status") != "ready" {
		return store.ErrBackupState
	}
	return s.backups.RecordBackupDelivery(ctx, relationship, artifact.Generation, artifact.Checksum, store.NodeAudit{Actor: "native-backup"})
}

func backupDue(success, attempt, now int64) bool {
	interval := int64(backupInterval / time.Second)
	if attempt > success {
		interval = int64(backupRetry / time.Second)
	}
	return now-max(success, attempt) >= interval
}

// RunBackups is independent of the heartbeat loop and uses a separate HTTPS
// connection pool. One OS lease and a serial worker bound database scans and
// CPU/memory pressure across all Followers and local runtime processes.
func (s *Service) RunBackups(ctx context.Context) {
	if s.backupBuilder == nil || s.backups == nil || s.pool == nil {
		return
	}
	release, err := s.backupBuilder.SchedulerLease(ctx)
	if err != nil {
		if ctx.Err() == nil {
			slog.Error("backup scheduler lease unavailable")
		}
		return
	}
	defer release()
	attempts := map[string]int64{}
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		if ctx.Err() != nil {
			return
		}
		identity, err := s.repo.ReadIdentity(ctx)
		if err == nil && identity.Role == "Master" {
			members, err := s.pool.Members(ctx)
			if err != nil {
				slog.Warn("backup scheduling state unavailable")
			} else {
				active := map[string]bool{}
				for _, member := range members {
					if member.RelationshipID == nil {
						continue
					}
					id := *member.RelationshipID
					active[id] = true
					enabled, _ := intField(member.Backup, "enabled")
					success, _ := intField(member.Backup, "last_success")
					if enabled != 1 || !backupDue(success, attempts[id], time.Now().Unix()) {
						continue
					}
					if _, err := s.backupRelation(ctx, id, nil); err != nil {
						continue
					}
					attempts[id] = time.Now().Unix()
					delivery, cancel := context.WithTimeout(ctx, 30*time.Minute)
					err := s.deliverBackup(delivery, id)
					cancel()
					if err != nil && ctx.Err() == nil {
						slog.Warn("business backup delivery incomplete", "relationship_id", id)
					}
					if ctx.Err() != nil {
						return
					}
				}
				for id := range attempts {
					if !active[id] {
						delete(attempts, id)
					}
				}
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
