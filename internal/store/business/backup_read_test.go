package business_test

import (
	"bytes"
	"context"
	"errors"
	"io"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestBusinessBackupColdReadSnapshotRetentionCorruptionAndLocalHistory(t *testing.T) {
	db, raw, rel := backupFixture(t)
	ctx, repo := context.Background(), db.Backups()
	payload := bytes.Repeat([]byte("\x00backup备份\xff"), 40_000)
	write := func(generation int64) {
		t.Helper()
		if err := repo.BeginBackup(ctx, rel.ID, generation, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		for offset, index := 0, 0; offset < len(payload); index++ {
			end := min(len(payload), offset+store.MaxBackupChunk)
			if err := repo.AppendBackup(ctx, rel.ID, generation, index, payload[offset:end]); err != nil {
				t.Fatal(err)
			}
			offset = end
		}
		if _, err := repo.CommitBackup(ctx, rel.ID, generation, backupChecksum(payload), store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
	}
	write(100)
	m, err := repo.ReadReadyBackup(ctx, rel.PeerID, 100, func(manifest store.BackupManifest, r io.Reader) error {
		first := make([]byte, 1)
		if _, err := io.ReadFull(r, first); err != nil {
			return err
		}
		// Retention removes this generation from the live DB during the read.
		// The same repeatable snapshot must still supply its complete chunks.
		write(101)
		write(102)
		rest, err := io.ReadAll(r)
		if err != nil {
			return err
		}
		if !bytes.Equal(append(first, rest...), payload) {
			t.Fatal("mixed cold snapshot")
		}
		return nil
	})
	if err != nil || m.Generation != 100 || m.Bytes != int64(len(payload)) {
		t.Fatal(m, err)
	}
	if _, err := repo.ReadReadyBackup(ctx, rel.PeerID, 100, func(store.BackupManifest, io.Reader) error { t.Fatal("retained artifact leaked"); return nil }); !errors.Is(err, store.ErrBackupState) {
		t.Fatal(err)
	}
	read := func() (store.BackupManifest, error) {
		return repo.ReadReadyBackup(ctx, rel.PeerID, 102, func(_ store.BackupManifest, r io.Reader) error { _, err := io.Copy(io.Discard, r); return err })
	}
	if _, err := repo.ReadReadyBackup(ctx, rel.PeerID, 102, func(_ store.BackupManifest, r io.Reader) error { p := make([]byte, 1); _, err := r.Read(p); return err }); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("prefix claimed valid", err)
	}
	sentinel := errors.New("consumer failed")
	if _, err := repo.ReadReadyBackup(ctx, rel.PeerID, 102, func(store.BackupManifest, io.Reader) error { return sentinel }); !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
	for _, statement := range []string{
		"UPDATE cluster_business_backups SET chunk_count=chunk_count+1 WHERE master_id=? AND generation=102",
		"UPDATE cluster_business_backups SET size_bytes=size_bytes+1 WHERE master_id=? AND generation=102",
		"UPDATE cluster_business_backups SET checksum='0000000000000000000000000000000000000000000000000000000000000000' WHERE master_id=? AND generation=102",
	} {
		if _, err := raw.Exec(statement, rel.PeerID); err != nil {
			t.Fatal(err)
		}
		if result, err := read(); !errors.Is(err, store.ErrBackupState) || result.Generation != 0 {
			t.Fatal("corrupt manifest proof", result, err)
		}
		if _, err := raw.Exec("UPDATE cluster_business_backups SET chunk_count=?,size_bytes=?,checksum=? WHERE master_id=? AND generation=102", (len(payload)+store.MaxBackupChunk-1)/store.MaxBackupChunk, len(payload), backupChecksum(payload), rel.PeerID); err != nil {
			t.Fatal(err)
		}
	}
	var original []byte
	if err := raw.QueryRow("SELECT payload FROM cluster_business_backup_chunks WHERE master_id=? AND generation=102 AND chunk_index=0", rel.PeerID).Scan(&original); err != nil {
		t.Fatal(err)
	}
	changed := bytes.Clone(original)
	changed[0] ^= 1
	if _, err := raw.Exec("UPDATE cluster_business_backup_chunks SET payload=? WHERE master_id=? AND generation=102 AND chunk_index=0", changed, rel.PeerID); err != nil {
		t.Fatal(err)
	}
	// Ignoring the stream's integrity error must not escape as a success.
	if _, err := repo.ReadReadyBackup(ctx, rel.PeerID, 102, func(_ store.BackupManifest, r io.Reader) error { io.Copy(io.Discard, r); return nil }); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("ignored checksum error", err)
	}
	if _, err := raw.Exec("UPDATE cluster_business_backup_chunks SET payload=? WHERE master_id=? AND generation=102 AND chunk_index=0", original, rel.PeerID); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("UPDATE cluster_business_backup_chunks SET payload=? WHERE master_id=? AND generation=102 AND chunk_index=0", bytes.Repeat([]byte{0}, store.MaxBackupChunk+10), rel.PeerID); err != nil {
		t.Fatal(err)
	}
	if _, err := read(); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("out-of-band oversized chunk accepted", err)
	}
	if _, err := raw.Exec("UPDATE cluster_business_backup_chunks SET payload=? WHERE master_id=? AND generation=102 AND chunk_index=0", original, rel.PeerID); err != nil {
		t.Fatal(err)
	}
	if err := db.Nodes().RevokeRelationship(ctx, rel.ID, false, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err := read(); err != nil {
		t.Fatal("historical local inspection required current credentials", err)
	}
	if _, err := raw.Exec("DELETE FROM cluster_business_backup_chunks WHERE master_id=? AND generation=102 AND chunk_index=1", rel.PeerID); err != nil {
		t.Fatal(err)
	}
	if _, err := read(); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("chunk gap accepted", err)
	}
	cancelled, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := repo.ReadReadyBackup(cancelled, rel.PeerID, 102, func(store.BackupManifest, io.Reader) error { return nil }); !errors.Is(err, context.Canceled) {
		t.Fatal("cancellation ignored", err)
	}
}
