package business

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// A repeatable cold snapshot survives concurrent retention. It deliberately
// does not require a still-active relationship: historical recovery artifacts
// are inspected by local maintenance, never used to authorize network access.
func (r *Repository) ReadReadyBackup(ctx context.Context, master string, generation int64, consume func(store.BackupManifest, io.Reader) error) (store.BackupManifest, error) {
	var empty store.BackupManifest
	if !nodeIDPattern.MatchString(master) || generation <= 0 || consume == nil {
		return empty, store.ErrBackupState
	}
	level := sql.LevelSerializable
	if r.backend == "mysql" {
		level = sql.LevelRepeatableRead
	}
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{Isolation: level, ReadOnly: true})
	if err != nil {
		return empty, err
	}
	defer tx.Rollback()
	m, err := scanBackup(tx.QueryRowContext(ctx, "SELECT "+backupColumns+" FROM cluster_business_backups WHERE master_id=? AND generation=?", master, generation))
	if errors.Is(err, sql.ErrNoRows) {
		err = store.ErrBackupState
	}
	if err != nil {
		return empty, err
	}
	if m.State != "ready" || !nodeHashPattern.MatchString(m.Checksum) || m.Bytes < 0 || m.Chunks < 1 || m.Chunks > store.MaxBackupChunkIndex+1 || m.Bytes > int64(m.Chunks)*store.MaxBackupChunk {
		return empty, store.ErrBackupState
	}
	// Bound even a corrupt out-of-band SQL payload before the driver allocates
	// it. LENGTH/SUBSTR on BLOBs have the same byte semantics in both drivers.
	query := fmt.Sprintf("SELECT chunk_index,LENGTH(payload),SUBSTR(payload,1,%d) FROM cluster_business_backup_chunks WHERE master_id=? AND generation=? ORDER BY chunk_index", store.MaxBackupChunk+1)
	rows, err := tx.QueryContext(ctx, query, master, generation)
	if err != nil {
		return empty, err
	}
	defer rows.Close()
	reader := &coldBackupReader{ctx: ctx, rows: rows, manifest: m, digest: sha256.New()}
	if err := consume(m, reader); err != nil {
		return empty, err
	}
	// A consumer cannot turn a prefix (or an ignored reader error) into proof.
	if err := ctx.Err(); err != nil {
		return empty, err
	}
	if !reader.verified || reader.failure != nil {
		return empty, store.ErrBackupState
	}
	if err := rows.Close(); err != nil {
		return empty, err
	}
	if err := tx.Commit(); err != nil {
		return empty, err
	}
	return m, nil
}

type coldBackupReader struct {
	ctx      context.Context
	rows     *sql.Rows
	manifest store.BackupManifest
	digest   hash.Hash
	chunk    []byte
	count    int
	size     int64
	verified bool
	failure  error
}

func (r *coldBackupReader) Read(p []byte) (n int, err error) {
	defer func() {
		if err != nil && err != io.EOF {
			r.failure = err
		}
	}()
	if r.failure != nil {
		return 0, r.failure
	}
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	if len(p) == 0 {
		return 0, nil
	}
	if r.verified {
		return 0, io.EOF
	}
	for len(r.chunk) == 0 {
		if !r.rows.Next() {
			if err := r.rows.Err(); err != nil {
				return 0, err
			}
			if r.count != r.manifest.Chunks || r.size != r.manifest.Bytes || hex.EncodeToString(r.digest.Sum(nil)) != r.manifest.Checksum {
				return 0, store.ErrBackupState
			}
			r.verified = true
			return 0, io.EOF
		}
		var index int
		var length int64
		if err := r.rows.Scan(&index, &length, &r.chunk); err != nil {
			return 0, err
		}
		if index != r.count || r.count >= r.manifest.Chunks || length != int64(len(r.chunk)) || len(r.chunk) > store.MaxBackupChunk || int64(len(r.chunk)) > r.manifest.Bytes-r.size {
			return 0, store.ErrBackupState
		}
		r.count++
		r.size += int64(len(r.chunk))
		r.digest.Write(r.chunk)
	}
	n = copy(p, r.chunk)
	r.chunk = r.chunk[n:]
	return n, nil
}
