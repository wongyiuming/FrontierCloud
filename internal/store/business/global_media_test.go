package business_test

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestGlobalReservationsFolderAffinityIdempotentFinalizeAndCapacity(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	pool, nodes := db.Pool(), db.Nodes()
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	row, err := nodes.InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "encrypted-test-fixture", CreatedAt: time.Now().Unix()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = nodes.PromoteIdentity(ctx, store.NodePromotion{Role: "Master", Endpoint: "https://master.test", Allocation: 20 * store.GiB, PhysicalFree: 5 * store.GiB}, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, memberID := range []string{row.ID, strings.Repeat("6", 32), strings.Repeat("7", 32)} {
			sqlDB.Exec("DELETE FROM cluster_upload_sessions WHERE storage_member_id=?", memberID)
			sqlDB.Exec("DELETE FROM global_media_objects WHERE storage_member_id=?", memberID)
			for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
				sqlDB.Exec("DELETE FROM "+table+" WHERE member_id=?", memberID)
			}
		}
		sqlDB.Exec("DELETE FROM node_relationships WHERE relationship_id IN (?,?)", strings.Repeat("8", 32), strings.Repeat("9", 32))
		sqlDB.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", row.Role, row.Endpoint)
	})
	for i := range 2 {
		memberID := strings.Repeat(fmt.Sprint(i+6), 32)
		relationID := strings.Repeat(fmt.Sprint(i+8), 32)
		v := store.Relationship{ID: relationID, PeerID: memberID, Endpoint: fmt.Sprintf("https://follower%d.test", i), PublicKey: strings.Repeat("A", 43), Credential: "encrypted-credential", Direction: "downstream", Mode: "Relay", State: "pending", Protocol: 2, CreatedAt: time.Now().Unix()}
		if err = nodes.PrepareRelationship(ctx, v, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		if err = nodes.ActivateRelationship(ctx, v.ID, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		if err = nodes.SetRelationshipMode(ctx, v.ID, "Direct", false, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		c := store.ResourceConfiguration{}
		c.Storage.Enabled = true
		c.Storage.Allocation = 10 * store.GiB
		if err = pool.ConfigureMember(ctx, memberID, c, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		if err = nodes.RecordHeartbeat(ctx, v.ID, true, 1, map[string]any{"storage": map[string]any{"physical_free_bytes": 20 * store.GiB}}, time.Now().Unix()); err != nil {
			t.Fatal(err)
		}
	}
	var workers sync.WaitGroup
	var success atomic.Int32
	failures := make(chan error, 32)
	results := make(chan store.UploadReservation, 16)
	for range 16 {
		workers.Go(func() {
			v, err := pool.ReserveUpload(ctx, "music/GlobalAffinity/track.mp3", "direct", 7, 5*store.GiB, store.AdminAudit{RequestID: "reservation-test"})
			if err == nil {
				success.Add(1)
				results <- v
			} else if !errors.Is(err, store.ErrNodeState) {
				failures <- err
			}
		})
	}
	workers.Wait()
	close(results)
	if success.Load() != 1 {
		t.Fatalf("duplicate path accepted %d times", success.Load())
	}
	first := <-results
	if first.MemberID != strings.Repeat("6", 32) {
		t.Fatalf("placement tie breaker %+v", first)
	}
	second, err := pool.ReserveUpload(ctx, "music/GlobalAffinity/other.mp3", "direct", 11, 5*store.GiB, store.AdminAudit{})
	if err != nil || second.MemberID != first.MemberID {
		t.Fatal("folder affinity lost", second, err)
	}
	if _, err = pool.ReserveUpload(ctx, "music/GlobalAffinity/nested/track.mp3", "direct", 1, 5*store.GiB, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("mixed layout accepted", err)
	}
	if _, err = pool.ReserveUpload(ctx, "music/GlobalAffinity/other-site.mp3", "primary", 1, 5*store.GiB, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("site affinity lost", err)
	}
	if _, err = pool.FinalizeUpload(ctx, first.ID, first.MediaID, 8, `"digest"`, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("size mismatch accepted", err)
	}
	for range 16 {
		workers.Go(func() {
			_, err := pool.FinalizeUpload(ctx, first.ID, first.MediaID, 7, `"digest"`, store.AdminAudit{RequestID: "finalize-test"})
			if err != nil {
				failures <- err
			}
		})
	}
	workers.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	object, err := pool.Resource(ctx, first.MediaID)
	if err != nil || object.ObjectID != first.MediaID || object.MemberID != first.MemberID || object.Path != first.Path {
		t.Fatalf("global catalog %+v %v", object, err)
	}
	objects, err := pool.Resources(ctx, "music/GlobalAffinity", false)
	if err != nil || len(objects) != 1 {
		t.Fatal("scoped global catalog", objects, err)
	}
	// Global events are Master-owned and must never manufacture a local
	// media_objects row for a remote Follower placement.
	playFailures := make(chan error, 32)
	for range 16 {
		workers.Go(func() {
			_, err := pool.RecordGlobalPlayback(ctx, first.MediaID, "71ab2e9c-429f-490b-a0ae-85eaa7656a30")
			if err != nil {
				playFailures <- err
			}
		})
	}
	workers.Wait()
	close(playFailures)
	for err := range playFailures {
		t.Fatal(err)
	}
	preference, err := pool.SetGlobalPreference(ctx, first.MediaID, 42, store.AdminAudit{Action: "media-priority", RequestID: "global-priority-test"})
	if err != nil || preference.PlayScore != 1 || preference.Preference != 42 {
		t.Fatal("global stats", preference, err)
	}
	fallback := store.MediaObject{Path: "lyrics/default.lrc", Kind: "lyric"}
	if err = pool.BindGlobalLyric(ctx, first.MediaID, fallback); err != nil {
		t.Fatal("global default lyric", err)
	}
	lyric := store.MediaObject{Path: "lyrics/GlobalAffinity/song.lrc", Kind: "lyric"}
	if _, err = db.Media().ReplaceLyricRelations(ctx, store.MediaObject{ID: first.MediaID, Path: first.Path, Kind: "audio"}, []store.MediaObject{lyric}, fallback, store.AdminAudit{}); err != nil {
		t.Fatal("global explicit lyric", err)
	}
	relations, err := db.Media().LyricRelations(ctx, "music/GlobalAffinity", "lyrics/GlobalAffinity")
	if err != nil || len(relations) != 1 || relations[0].Track != first.Path || relations[0].Lyric != lyric.Path {
		t.Fatal("global lyric relations", relations, err)
	}
	auto, err := db.Media().AutoLyricRelations(ctx, []store.LyricPair{{Track: store.MediaObject{ID: first.MediaID, Path: first.Path, Kind: "audio"}, Lyric: lyric}}, store.AutoLyricResult{}, store.AdminAudit{})
	if err != nil || auto.Preserved != 1 || auto.Linked != 0 {
		t.Fatal("global auto relation identity", auto, err)
	}
	var localRows int
	if err = sqlDB.QueryRow("SELECT COUNT(*) FROM media_objects WHERE media_id=?", first.MediaID).Scan(&localRows); err != nil || localRows != 0 {
		t.Fatal("remote placement turned into a local media identity", localRows, err)
	}
	var count int
	if err = sqlDB.QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE request_id='finalize-test' AND action='upload-finalized'").Scan(&count); err != nil || count != 1 {
		t.Fatal("duplicate completion audit", count, err)
	}
	members, err := pool.Members(ctx)
	if err != nil {
		t.Fatal(err)
	}
	for _, v := range members {
		if v.ID == first.MemberID && (v.Used != 7 || v.Reserved != 11) {
			t.Fatalf("capacity accounting %+v", v)
		}
	}
	if err = pool.ReleaseCleanedUpload(ctx, first.ID, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("completed object reservation released", err)
	}
	if err = pool.ReleaseCleanedUpload(ctx, second.ID, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if err = pool.ReleaseCleanedUpload(ctx, second.ID, store.AdminAudit{}); err != nil {
		t.Fatal("cleanup replay", err)
	}
	// Expiry does not mean that the physical object disappeared. Keep the path
	// and quota reserved until the service confirms cleanup of its placement.
	expired, err := pool.ReserveUpload(ctx, "music/GlobalExpired/track.mp3", "direct", 13, 5*store.GiB, store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = sqlDB.Exec("UPDATE cluster_upload_sessions SET expires_at=? WHERE upload_id=?", time.Now().Unix()-1, expired.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.FinalizeUpload(ctx, expired.ID, expired.MediaID, 13, `"digest"`, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("expired finalized", err)
	}
	if _, err = pool.ReserveUpload(ctx, expired.Path, "direct", 13, 5*store.GiB, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("expired unknown physical placement reused", err)
	}
	expiredList, err := pool.ExpiredUploads(ctx, 50)
	if err != nil || len(expiredList) != 1 || expiredList[0].ID != expired.ID {
		t.Fatal("expiry recovery inventory", expiredList, err)
	}
	if err = pool.ReleaseCleanedUpload(ctx, expired.ID, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	// Logical quota is 20 GiB, but only 4 GiB is physically writable. Pending
	// transfers must consume the physical ceiling too.
	physical, err := pool.ReserveUpload(ctx, "music/GlobalPhysical/large.mp3", "primary", 3*store.GiB, 5*store.GiB, store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = pool.ReserveUpload(ctx, "music/GlobalPhysical/second.mp3", "primary", 2*store.GiB, 5*store.GiB, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("physical volume oversubscribed", err)
	}
	if err = pool.ReleaseCleanedUpload(ctx, physical.ID, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
}

func TestGlobalReservationAndFinalizationAuditFailurePreserveFunds(t *testing.T) {
	db := database(t)
	if db.Backend() != "sqlite" {
		t.Skip("SQLite audit failure injection")
	}
	ctx := context.Background()
	pool, nodes := db.Pool(), db.Nodes()
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	row, err := nodes.InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "encrypted-fixture"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = nodes.PromoteIdentity(ctx, store.NodePromotion{Role: "Master", Endpoint: "https://master.test", Allocation: 2 * store.GiB, PhysicalFree: 5 * store.GiB}, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err = sqlDB.Exec("CREATE TRIGGER global_audit_fault BEFORE INSERT ON admin_audit_log BEGIN SELECT RAISE(FAIL,'audit unavailable'); END"); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.ReserveUpload(ctx, "music/AtomicGlobal/track.mp3", "primary", 7, 5*store.GiB, store.AdminAudit{}); err == nil {
		t.Fatal("unaudited reservation committed")
	}
	var reserved int64
	if err = sqlDB.QueryRow("SELECT reserved_bytes FROM cluster_storage_members WHERE member_id=?", row.ID).Scan(&reserved); err != nil || reserved != 0 {
		t.Fatal("reservation survived rollback", reserved, err)
	}
	if _, err = sqlDB.Exec("DROP TRIGGER global_audit_fault"); err != nil {
		t.Fatal(err)
	}
	v, err := pool.ReserveUpload(ctx, "music/AtomicGlobal/track.mp3", "primary", 7, 5*store.GiB, store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = sqlDB.Exec("CREATE TRIGGER global_audit_fault BEFORE INSERT ON admin_audit_log BEGIN SELECT RAISE(FAIL,'audit unavailable'); END"); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.FinalizeUpload(ctx, v.ID, v.MediaID, 7, `"digest"`, store.AdminAudit{}); err == nil {
		t.Fatal("unaudited completion committed")
	}
	v, err = pool.Upload(ctx, v.ID)
	if err != nil || v.State != "reserved" || v.Member.Reserved != 7 || v.Member.Used != 0 {
		t.Fatalf("capacity lost on rollback %+v %v", v, err)
	}
	var count int
	if err = sqlDB.QueryRow("SELECT COUNT(*) FROM global_media_objects").Scan(&count); err != nil || count != 0 {
		t.Fatal("orphan global placement", count, err)
	}
}
