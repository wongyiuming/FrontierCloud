package business_test

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestRecordingQuotaConcurrentFinalizeDeletionAndCrashStates(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	raw := db.(interface{ Database() *sql.DB }).Database()
	n, e := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "fixture", CreatedAt: time.Now().Unix()})
	if e != nil {
		t.Fatal(e)
	}
	if _, e = db.Nodes().PromoteIdentity(ctx, store.NodePromotion{Role: "Master", Endpoint: "https://recording.test", Allocation: 5 * store.GiB, PhysicalFree: 10 * store.GiB}, store.NodeAudit{}); e != nil {
		t.Fatal(e)
	}
	user := store.KaraokeUser{ID: strings.Repeat("d", 32), Username: "native-recordings", NameKey: "native-recordings", PasswordHash: "fixture", Quota: 3 * 1024 * 1024}
	audit := store.KaraokeAudit{IP: "192.0.2.175"}
	t.Cleanup(func() {
		raw.Exec("DELETE FROM karaoke_recordings WHERE user_id=?", user.ID)
		raw.Exec("DELETE FROM karaoke_users WHERE user_id=?", user.ID)
		raw.Exec("DELETE FROM karaoke_audit_log WHERE client_ip=?", audit.IP)
		raw.Exec("DELETE FROM karaoke_registration_daily WHERE client_ip=?", audit.IP)
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			raw.Exec("DELETE FROM "+table+" WHERE member_id=?", n.ID)
		}
		raw.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", n.Role, n.Endpoint)
	})
	if e = db.Karaoke().RegisterUser(ctx, user, audit.IP, "20261001", audit); e != nil {
		t.Fatal(e)
	}
	repo := db.Recordings()
	var wg sync.WaitGroup
	var success atomic.Int32
	ids := make(chan string, 16)
	for i := range 16 {
		wg.Go(func() {
			id := fmt.Sprintf("%032x", 10000+i)
			v, _, e := repo.ReserveRecording(ctx, store.Recording{ID: id, UserID: user.ID, Filename: "录音.webm", ContentType: "audio/webm", Bytes: 1024 * 1024}, 10*store.GiB, audit)
			if e == nil {
				success.Add(1)
				ids <- v.ID
			} else if !errors.Is(e, store.ErrRecordingQuota) {
				t.Error(e)
			}
		})
	}
	wg.Wait()
	close(ids)
	if success.Load() != 3 {
		t.Fatal("quota raced", success.Load())
	}
	id := <-ids
	digest := strings.Repeat("b", 64)
	receipt := store.RecordingReceipt{ID: id, Bytes: 1024 * 1024, SHA256: digest, Metadata: &store.RecordingMetadata{Title: "本地尾部标题", Lyrics: []store.RecordingLyric{{Time: 1.2, Text: "歌词"}}}}
	if db.Backend() == "sqlite" {
		if _, e = raw.Exec("CREATE TRIGGER recording_finalize_audit_failure BEFORE INSERT ON karaoke_audit_log WHEN NEW.action='recording-finalize' BEGIN SELECT RAISE(ABORT,'audit fixture'); END"); e != nil {
			t.Fatal(e)
		}
		err := repo.FinalizeRecording(ctx, user.ID, id, receipt, audit)
		if _, e = raw.Exec("DROP TRIGGER recording_finalize_audit_failure"); e != nil {
			t.Fatal(e)
		}
		if err == nil {
			t.Fatal("audit failure not propagated")
		}
		row, e := repo.Recording(ctx, id)
		if e != nil || row.State != "pending" || row.SHA256 != nil {
			t.Fatal("failed audit committed publication", row, e)
		}
	}
	for range 16 {
		wg.Go(func() {
			if e := repo.FinalizeRecording(ctx, user.ID, id, receipt, audit); e != nil {
				t.Error(e)
			}
		})
	}
	wg.Wait()
	v, e := repo.Recording(ctx, id)
	if e != nil || v.State != "ready" || v.Title != "本地尾部标题" || v.SHA256 == nil || *v.SHA256 != digest {
		t.Fatal(v, e)
	}
	var count int
	if e = raw.QueryRow("SELECT COUNT(*) FROM karaoke_audit_log WHERE user_id=? AND action='recording-finalize'", user.ID).Scan(&count); e != nil || count != 1 {
		t.Fatal("duplicate finalization audit", count, e)
	}
	if _, e = repo.StageRecordingDeletion(ctx, user.ID, id, true, audit); !errors.Is(e, store.ErrRecordingState) {
		t.Fatal("ready cancellation", e)
	}
	if e = repo.CompleteRecordingDeletion(ctx, user.ID, id, audit); !errors.Is(e, store.ErrRecordingState) {
		t.Fatal("delete without durable intent", e)
	}
	if _, e = repo.StageRecordingDeletion(ctx, user.ID, id, false, audit); e != nil {
		t.Fatal(e)
	}
	if e = repo.FinalizeRecording(ctx, user.ID, id, receipt, audit); !errors.Is(e, store.ErrRecordingState) {
		t.Fatal("deleting recording finalized", e)
	}
	for range 16 {
		wg.Go(func() {
			if e := repo.CompleteRecordingDeletion(ctx, user.ID, id, audit); e != nil {
				t.Error(e)
			}
		})
	}
	wg.Wait()
	u, e := db.Karaoke().UserByID(ctx, user.ID)
	if e != nil || u.Used != 2*1024*1024 {
		t.Fatal("duplicate refund", u, e)
	}
	for pendingID := range ids {
		if _, e = repo.StageRecordingDeletion(ctx, user.ID, pendingID, true, audit); e != nil {
			t.Fatal(e)
		}
		if e = repo.CompleteRecordingDeletion(ctx, user.ID, pendingID, audit); e != nil {
			t.Fatal(e)
		}
	}
	u, e = db.Karaoke().UserByID(ctx, user.ID)
	if e != nil || u.Used != 0 {
		t.Fatal(u, e)
	}
	members, e := db.Pool().Members(ctx)
	if e != nil || len(members) != 1 || members[0].Used != 0 || members[0].Reserved != 0 {
		t.Fatal("member refund", members, e)
	}
	stale, _, e := repo.ReserveRecording(ctx, store.Recording{ID: strings.Repeat("e", 32), UserID: user.ID, Filename: "stale.webm", ContentType: "audio/webm", Bytes: 100}, 10*store.GiB, audit)
	v = &stale
	if e != nil {
		t.Fatal(e)
	}
	if _, e = raw.Exec("UPDATE karaoke_recordings SET updated_at=? WHERE recording_id=?", time.Now().Unix()-3601, v.ID); e != nil {
		t.Fatal(e)
	}
	if e = repo.StageExpiredRecordings(ctx, 10); e != nil {
		t.Fatal(e)
	}
	v, e = repo.Recording(ctx, v.ID)
	if e != nil || v.State != "deleting" {
		t.Fatal(v, e)
	}
	u, e = db.Karaoke().UserByID(ctx, user.ID)
	if e != nil || u.Used != 100 {
		t.Fatal("expiry is not physical cleanup proof", u, e)
	}
	if _, e = raw.Exec("UPDATE karaoke_users SET used_bytes=99 WHERE user_id=?", user.ID); e != nil {
		t.Fatal(e)
	}
	if e = repo.CompleteRecordingDeletion(ctx, user.ID, v.ID, audit); !errors.Is(e, store.ErrRecordingState) {
		t.Fatal("accounting silently clamped", e)
	}
	if _, e = raw.Exec("UPDATE karaoke_users SET used_bytes=100 WHERE user_id=?", user.ID); e != nil {
		t.Fatal(e)
	}
	if e = repo.CompleteRecordingDeletion(ctx, user.ID, v.ID, audit); e != nil {
		t.Fatal(e)
	}
}
