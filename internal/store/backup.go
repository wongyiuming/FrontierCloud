package store

import (
	"context"
	"errors"
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
	ExportBusinessSnapshot(context.Context, func(string, map[string]any) error) error
	RecordBackupDelivery(context.Context, string, int64, string, NodeAudit) error
	BeginBackup(context.Context, string, int64, NodeAudit) error
	AppendBackup(context.Context, string, int64, int, []byte) error
	CommitBackup(context.Context, string, int64, string, NodeAudit) (BackupManifest, error)
	AbortBackups(context.Context, string, int64, NodeAudit) (BackupAbort, error)
}
