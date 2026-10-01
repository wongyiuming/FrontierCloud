package httpapi

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/backup"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type backupHTTP struct {
	*clusterHTTP
	after func(string, map[string]any, error) (map[string]any, error)
}

func (t *backupHTTP) Request(ctx context.Context, origin, route, method string, value any, relationship, credential string) (map[string]any, error) {
	result, err := t.clusterHTTP.Request(ctx, origin, route, method, value, relationship, credential)
	if t.after != nil {
		return t.after(route, result, err)
	}
	return result, err
}
func backupDeliveryFixture(t *testing.T) (recordingNode, recordingNode, *backup.Builder, *backupHTTP, store.Relationship, *node.Service) {
	t.Helper()
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	master := nativeRecordingFixture(t, "https://delivery-master.test", "Master", transport)
	follower := nativeRecordingFixture(t, "https://delivery-follower.test", "Follower", transport)
	pack, err := follower.control.CreatePair(ctx, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	id, err := master.control.ImportPair(ctx, pack, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	rel, err := master.db.Nodes().Relationship(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	cfg := store.ResourceConfiguration{}
	cfg.Backup.Enabled = true
	if err := master.db.Pool().ConfigureMember(ctx, follower.id, cfg, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := master.control.Tick(ctx, rel); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(master.dir, "media", "lyrics", "large.lrc"), []byte(strings.Repeat("[00:01]跨语言备份\n", 20_000)), 0600); err != nil {
		t.Fatal(err)
	}
	builder, err := backup.New(master.db.Backups(), master.public.media, filepath.Join(master.dir, ".business-backups"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { builder.Close() })
	identity, err := node.Initialize(ctx, master.db.Nodes(), filepath.Join(master.dir, "secrets"))
	if err != nil {
		t.Fatal(err)
	}
	fault := &backupHTTP{clusterHTTP: transport}
	sender := node.NewService(master.db.Nodes(), identity, fault)
	root, err := os.OpenRoot(filepath.Join(master.dir, "recordings"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { root.Close() })
	sender.ConfigureVolumes(master.db.Pool(), master.public.media, root)
	sender.ConfigureBackups(master.db.Backups(), builder)
	return master, follower, builder, fault, rel, sender
}

func assertNoBackupArtifact(t *testing.T, master recordingNode) {
	t.Helper()
	entries, err := os.ReadDir(filepath.Join(master.dir, ".business-backups"))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), "business-") {
			t.Fatal("sensitive temporary artifact retained", entry.Name())
		}
	}
}

func TestBusinessBackupDeliveryNativeSignedChunksAndVerifiedMasterReceipt(t *testing.T) {
	master, follower, _, _, rel, sender := backupDeliveryFixture(t)
	ctx := context.Background()
	if err := sender.DeliverBackup(ctx, rel.ID); err != nil {
		t.Fatal(err)
	}
	var generation, size int64
	var checksum, state string
	var count int
	if err := follower.db.Database().QueryRow("SELECT generation,checksum,size_bytes,chunk_count,state FROM cluster_business_backups WHERE master_id=?", master.id).Scan(&generation, &checksum, &size, &count, &state); err != nil || state != "ready" || count < 2 {
		t.Fatal("remote manifest", generation, size, count, state, err)
	}
	rows, err := follower.db.Database().Query("SELECT payload FROM cluster_business_backup_chunks WHERE master_id=? AND generation=? ORDER BY chunk_index", master.id, generation)
	if err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	for rows.Next() {
		var payload []byte
		if err := rows.Scan(&payload); err != nil {
			t.Fatal(err)
		}
		if len(payload) > store.MaxBackupChunk {
			t.Fatal("unbounded chunk")
		}
		output.Write(payload)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(output.Bytes())
	if checksum != hex.EncodeToString(hash[:]) || size != int64(output.Len()) {
		t.Fatal("checksum/size changed")
	}
	decoder := json.NewDecoder(bytes.NewReader(output.Bytes()))
	decoder.UseNumber()
	lyricSeen, endSeen := false, false
	for {
		var value map[string]any
		if err := decoder.Decode(&value); err == io.EOF {
			break
		} else if err != nil {
			t.Fatal(err)
		}
		if endSeen {
			t.Fatal("bytes after artifact footer")
		}
		if value["kind"] == "lyric" && value["name"] == "large.lrc" {
			lyricSeen = true
		}
		if value["kind"] == "end" {
			endSeen = true
		}
		if table, _ := value["table"].(string); table == "node_identity" || table == "node_relationships" {
			t.Fatal("private node identity exported")
		}
	}
	if !lyricSeen || !endSeen {
		t.Fatal("incomplete artifact", lyricSeen, endSeen)
	}
	var delivered int64
	if err := master.db.Database().QueryRow("SELECT generation FROM cluster_backup_members WHERE member_id=?", follower.id).Scan(&delivered); err != nil || delivered != generation {
		t.Fatal("Master claimed unverified generation", delivered, err)
	}
	for range 2 {
		if err := master.db.Backups().RecordBackupDelivery(ctx, rel.ID, generation, checksum, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
	}
	if err := master.db.Database().QueryRow("SELECT COUNT(*) FROM node_audit WHERE relationship_id=? AND action='backup-delivered'", rel.ID).Scan(&count); err != nil || count != 1 {
		t.Fatal("delivery audit replay", count, err)
	}
	if err := master.db.Backups().RecordBackupDelivery(ctx, rel.ID, generation, strings.Repeat("a", 64), store.NodeAudit{}); !errors.Is(err, store.ErrBackupState) {
		t.Fatal("receipt rebound", err)
	}
	assertNoBackupArtifact(t, master)
}

func TestBusinessBackupDeliveryLostBeginChunkAndCommitAcknowledgements(t *testing.T) {
	for _, operation := range []string{"begin", "chunk", "commit"} {
		t.Run(operation, func(t *testing.T) {
			master, follower, _, transport, rel, sender := backupDeliveryFixture(t)
			failed := false
			transport.after = func(route string, result map[string]any, err error) (map[string]any, error) {
				if route == "/internal/v1/backup/"+operation && err == nil && !failed {
					failed = true
					return nil, errors.New("lost backup acknowledgement")
				}
				return result, err
			}
			if err := sender.DeliverBackup(context.Background(), rel.ID); err == nil {
				t.Fatal("lost acknowledgement falsely succeeded")
			}
			var ready, receiving int
			if err := follower.db.Database().QueryRow("SELECT COUNT(*) FROM cluster_business_backups WHERE master_id=? AND state='receiving'", master.id).Scan(&receiving); err != nil || receiving != 0 {
				t.Fatal("partial generation not aborted", receiving, err)
			}
			if err := follower.db.Database().QueryRow("SELECT COUNT(*) FROM cluster_business_backups WHERE master_id=? AND state='ready'", master.id).Scan(&ready); err != nil {
				t.Fatal(err)
			}
			want := 0
			if operation == "commit" {
				want = 1
			}
			if ready != want {
				t.Fatal("verified remote generation erased", ready, want)
			}
			var success int64
			if err := master.db.Database().QueryRow("SELECT last_success FROM cluster_backup_members WHERE member_id=?", follower.id).Scan(&success); err != nil || success != 0 {
				t.Fatal("unacknowledged backup claimed", success, err)
			}
			assertNoBackupArtifact(t, master)
			if err := sender.DeliverBackup(context.Background(), rel.ID); err != nil {
				t.Fatal("new generation retry", err)
			}
			assertNoBackupArtifact(t, master)
		})
	}
}

func TestBusinessBackupDeliveryDoesNotHoldMediaLeaseOrHeartbeatPool(t *testing.T) {
	master, _, _, transport, rel, sender := backupDeliveryFixture(t)
	entered, proceed := make(chan struct{}), make(chan struct{})
	transport.after = func(route string, result map[string]any, err error) (map[string]any, error) {
		if route == "/internal/v1/backup/begin" && err == nil {
			close(entered)
			<-proceed
		}
		return result, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- sender.DeliverBackup(ctx, rel.ID) }()
	select {
	case <-entered:
	case <-ctx.Done():
		close(proceed)
		<-done
		t.Fatal("delivery did not enter transport")
	}
	probe, stop := context.WithTimeout(ctx, 2*time.Second)
	err := sender.Tick(probe, rel)
	if err == nil {
		_, _, err = master.public.media.PhysicalCapacity(probe)
	}
	stop()
	close(proceed)
	deliveryErr := <-done
	if err != nil || deliveryErr != nil {
		t.Fatal("backup stalled heartbeat/media traffic", err, deliveryErr)
	}
	assertNoBackupArtifact(t, master)
}

func TestBusinessBackupBackgroundFirstDeliveryAndJoinedCancellation(t *testing.T) {
	master, _, _, transport, _, sender := backupDeliveryFixture(t)
	committed := make(chan struct{}, 1)
	transport.after = func(route string, result map[string]any, err error) (map[string]any, error) {
		if route == "/internal/v1/backup/commit" && err == nil {
			committed <- struct{}{}
		}
		return result, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); sender.RunBackups(ctx) }()
	select {
	case <-committed:
	case <-time.After(5 * time.Second):
		cancel()
		<-done
		t.Fatal("background scheduler did not deliver")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("backup scheduler did not join shutdown")
	}
	assertNoBackupArtifact(t, master)
}

func TestBusinessBackupDeliveryDefersLiveReservationsAndRevokedRelationship(t *testing.T) {
	master, follower, _, transport, rel, sender := backupDeliveryFixture(t)
	ctx := context.Background()
	ticket, err := master.public.media.ReserveMasterUpload(ctx, "pending.mp3", "music/backup-test", "", "primary", 10, 255, store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	if err := sender.DeliverBackup(ctx, rel.ID); !errors.Is(err, store.ErrBackupBusy) {
		t.Fatal("unrecoverable reservation exported", err)
	}
	var count int
	if err := follower.db.Database().QueryRow("SELECT COUNT(*) FROM cluster_business_backups").Scan(&count); err != nil || count != 0 {
		t.Fatal("partial export sent to peer", count, err)
	}
	assertNoBackupArtifact(t, master)
	if err := master.public.media.CancelMasterUpload(ctx, ticket.ID, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	transport.after = func(route string, result map[string]any, err error) (map[string]any, error) {
		if route == "/internal/v1/backup/chunk" && err == nil {
			_, err = master.db.Database().Exec("UPDATE node_relationships SET state='revoked' WHERE relationship_id=?", rel.ID)
		}
		return result, err
	}
	if err := sender.DeliverBackup(ctx, rel.ID); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("revoked placement received commit", err)
	}
	if err := follower.db.Database().QueryRow("SELECT COUNT(*) FROM cluster_business_backups WHERE state='ready'").Scan(&count); err != nil || count != 0 {
		t.Fatal("revoked transfer marked ready", count, err)
	}
	assertNoBackupArtifact(t, master)
}

type delayedBackupRepository struct {
	store.BackupRepository
	entered, proceed chan struct{}
}

func (r delayedBackupRepository) ExportBusinessSnapshot(ctx context.Context, emit func(string, map[string]any) error) error {
	close(r.entered)
	select {
	case <-r.proceed:
		return r.BackupRepository.ExportBusinessSnapshot(ctx, emit)
	case <-ctx.Done():
		return ctx.Err()
	}
}

func TestBusinessBackupSnapshotSharedLeaseAllowsReadsButFencesPhysicalMutation(t *testing.T) {
	master, _, _, _, _, _ := backupDeliveryFixture(t)
	entered, proceed := make(chan struct{}), make(chan struct{})
	builder, err := backup.New(delayedBackupRepository{master.db.Backups(), entered, proceed}, master.public.media, filepath.Join(master.dir, ".delayed-backup"))
	if err != nil {
		t.Fatal(err)
	}
	defer builder.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		artifact, err := builder.Build(ctx)
		if artifact != nil {
			err = errors.Join(err, artifact.Close())
		}
		done <- err
	}()
	select {
	case <-entered:
	case <-ctx.Done():
		close(proceed)
		<-done
		t.Fatal("snapshot did not start")
	}
	read, stopRead := context.WithTimeout(ctx, time.Second)
	_, _, readErr := master.public.media.PhysicalCapacity(read)
	stopRead()
	write, stopWrite := context.WithTimeout(ctx, 100*time.Millisecond)
	called := false
	writeErr := master.public.media.WithPromotion(write, "Master", func(store.NodePromotion) error { called = true; return nil })
	stopWrite()
	close(proceed)
	buildErr := <-done
	if readErr != nil || buildErr != nil || !errors.Is(writeErr, context.DeadlineExceeded) || called {
		t.Fatal("snapshot lease boundary", readErr, buildErr, writeErr, called)
	}
}
