package store

import (
	"context"
	"errors"
	"io"
)

const MaxBackupChunk = 192 * 1024
const MaxBackupChunkIndex = 10_000_000

var ErrBackupState = errors.New("invalid or incomplete business backup")
var ErrBackupBusy = errors.New("business mutations must finish before backup")

type BackupManifest struct {
	MasterID             string
	Generation           int64
	Checksum             string
	Bytes                int64
	Chunks               int
	State                string
	CreatedAt, UpdatedAt int64
}

type BackupAbort struct {
	Status             string `json:"status"`
	Generation         int64  `json:"generation"`
	AbortedGenerations int    `json:"aborted_generations"`
}

// Backups are cold recovery artifacts, never an alternate source for online
// business reads. Every mutation rechecks the current upstream under a lock.
type BackupRepository interface {
	// ReadReadyBackup is local maintenance infrastructure, not online business
	// authority. consume must be side-effect free: bytes remain untrusted until
	// the callback AND the final snapshot/checksum checks succeed.
	ReadReadyBackup(context.Context, string, int64, func(BackupManifest, io.Reader) error) (BackupManifest, error)
	ExportBusinessSnapshot(context.Context, func(string, map[string]any) error) error
	RecordBackupDelivery(context.Context, string, int64, string, NodeAudit) error
	BeginBackup(context.Context, string, int64, NodeAudit) error
	AppendBackup(context.Context, string, int64, int, []byte) error
	CommitBackup(context.Context, string, int64, string, NodeAudit) (BackupManifest, error)
	AbortBackups(context.Context, string, int64, NodeAudit) (BackupAbort, error)
}

// BusinessBackupTables returns the existing v2 allowlist, never caller SQL.
// Return a new slice so consumers cannot mutate the protocol for other callers.
func BusinessBackupTables() []string {
	return []string{
		"media_visibility", "media_objects", "media_playback_stats", "media_playback_events",
		"media_lyric_links", "global_media_objects", "cluster_storage_members",
		"cluster_compute_members", "cluster_worker_jobs", "cluster_backup_members",
		"karaoke_users", "karaoke_recordings", "karaoke_registration_daily", "karaoke_audit_log",
		"admin_audit_log", "webrtc_observation_events", "webrtc_observation_summary",
		"ip_security_audit_log", "ip_security_summary", "ip_security_projection",
		"ip_auto_ban_events", "ip_permanent_whitelist", "node_audit",
	}
}
