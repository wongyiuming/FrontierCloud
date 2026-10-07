package business_test

import (
	"context"
	"database/sql"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestMasterUploadPublicationAtomicIdentityQuotaAndConcurrentReplay(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	d := db.(interface{ Database() *sql.DB }).Database()
	row, err := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "encrypted-fixture", CreatedAt: time.Now().Unix()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Nodes().PromoteIdentity(ctx, store.NodePromotion{Role: "Master", Endpoint: "https://master.test", Allocation: 5 * store.GiB, PhysicalFree: 10 * store.GiB}, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	var v store.UploadReservation
	t.Cleanup(func() {
		d.Exec("DELETE FROM admin_audit_log WHERE request_id=?", "master-native-transaction")
		d.Exec("DELETE FROM media_objects WHERE media_id=?", v.MediaID)
		d.Exec("DELETE FROM global_media_objects WHERE storage_member_id=?", row.ID)
		d.Exec("DELETE FROM cluster_upload_sessions WHERE storage_member_id=?", row.ID)
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			d.Exec("DELETE FROM "+table+" WHERE member_id=?", row.ID)
		}
		d.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", row.Role, row.Endpoint)
	})
	v, err = db.Pool().ReserveUpload(ctx, "music/NativeMasterPublication/song.mp3", "primary", 10, 10*store.GiB, store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	o := store.MediaObject{ID: v.MediaID, Path: v.Path, Kind: v.Kind}
	if err := db.Pool().CompleteMasterUpload(ctx, v.ID, o, 11, `"digest"`, 10*store.GiB, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("size conflict accepted", err)
	}
	if _, err := d.Exec("UPDATE cluster_upload_sessions SET expires_at=? WHERE upload_id=?", time.Now().Unix()-1, v.ID); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for range 16 {
		wg.Go(func() {
			if err := db.Pool().CompleteMasterUpload(ctx, v.ID, o, 10, `"digest"`, 9*store.GiB, store.AdminAudit{RequestID: "master-native-transaction"}); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	var used, reserved int64
	if err := d.QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", row.ID).Scan(&used, &reserved); err != nil || used != 10 || reserved != 0 {
		t.Fatal("capacity", used, reserved, err)
	}
	object, err := db.Media().ObjectByID(ctx, o.ID)
	if err != nil || object == nil || object.Path != o.Path {
		t.Fatal("local identity", object, err)
	}
	g, err := db.Pool().Resource(ctx, o.ID)
	if err != nil || g.ObjectID != o.ID || g.ID != o.ID || g.Bytes != 10 {
		t.Fatal("global identity", g, err)
	}
	var count int
	if err := d.QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='upload-finalized' AND request_id=?", "master-native-transaction").Scan(&count); err != nil || count != 1 {
		t.Fatal("exactly once audit", count, err)
	}
	if err := db.Pool().CompleteMasterUpload(ctx, v.ID, o, 10, `"different"`, 9*store.GiB, store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("completed object replacement", err)
	}
}
