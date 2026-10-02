package business_test

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestMaintenanceLogicalSnapshotDoesNotEraseIntentsOrReadCredentials(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	if _, err := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "private-fixture"}); err != nil {
		t.Fatal(err)
	}
	raw := db.(interface{ Database() *sql.DB }).Database()
	before, err := db.Maintenance().InspectMaintenance(ctx)
	if err != nil {
		t.Fatal(err)
	}
	id := strings.Repeat("7", 32)
	t.Cleanup(func() {
		raw.Exec("DELETE FROM media_delete_operations WHERE operation_id=?", id)
		raw.Exec("DELETE FROM cluster_business_backups WHERE master_id=?", id)
		raw.Exec("UPDATE frontiercloud_schema SET generation=2 WHERE singleton=1")
	})
	if _, err := raw.Exec("INSERT INTO media_delete_operations(operation_id,state,manifest,created_at) VALUES (?,'rename_pending','{}','2026-10-02 01:02:03.000000')", id); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("INSERT INTO cluster_business_backups(master_id,generation,checksum,size_bytes,chunk_count,state,created_at,updated_at) VALUES (?,1,'',0,0,'receiving',1,1)", id); err != nil {
		t.Fatal(err)
	}
	during, err := db.Maintenance().InspectMaintenance(ctx)
	if err != nil || during.LogicalIdle || during.Pending["media_mutations"] != before.Pending["media_mutations"]+1 || during.Pending["cold_transfers"] != before.Pending["cold_transfers"]+1 || during.Role != before.Role {
		t.Fatal(during, err)
	}
	if _, err := raw.Exec("UPDATE media_delete_operations SET state='rename_done' WHERE operation_id=?", id); err != nil {
		t.Fatal(err)
	}
	after, err := db.Maintenance().InspectMaintenance(ctx)
	if err != nil || after.CompletedNativeRenames != before.CompletedNativeRenames+1 || after.Pending["media_mutations"] != before.Pending["media_mutations"] {
		t.Fatal(after, err)
	}
	var state string
	if err := raw.QueryRow("SELECT state FROM cluster_business_backups WHERE master_id=?", id).Scan(&state); err != nil || state != "receiving" {
		t.Fatal("inspection discarded an intent", state, err)
	}
	if _, err := raw.Exec("UPDATE frontiercloud_schema SET generation=3 WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
	if result, err := db.Maintenance().InspectMaintenance(ctx); !errors.Is(err, store.ErrBackupState) || result.LogicalIdle {
		t.Fatal("future schema accepted", result, err)
	}
}
