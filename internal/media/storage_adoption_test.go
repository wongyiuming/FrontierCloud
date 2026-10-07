package media

import (
	"context"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLegacyOwnedStoragePhysicalAdoptionPreservesQuotaIDsAndRefusesUnprovenBytes(t *testing.T) {
	root, db, svc, relationship := ownedFixture(t)
	ctx := context.Background()
	n, err := db.Nodes().ReadIdentity(ctx)
	if err != nil {
		t.Fatal(err)
	}
	paths := []string{"music/artist/song.mp3", "music/other/song.mp3", "vido/director/video.mp4"}
	var objects []store.MediaObject
	for _, name := range paths {
		kind := "audio"
		if strings.HasPrefix(name, "vido/") {
			kind = "video"
		}
		objects = append(objects, store.MediaObject{Path: name, Kind: kind})
	}
	ids, err := db.Media().EnsureObjects(ctx, objects)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Database().Exec("UPDATE cluster_storage_members SET used_bytes=30 WHERE member_id=?", n.ID); err != nil {
		t.Fatal(err)
	}
	if err := svc.OwnedDelete(ctx, relationship, ids[paths[0]], paths[0], 10, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("legacy refund accepted without ledger", err)
	}
	foreign := filepath.Join(root, "music/artist/unknown.mp3")
	if err := os.WriteFile(foreign, []byte("ID3unknown"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := AdoptOwnedStorage(ctx, root, db.Maintenance(), db.Media(), db.Nodes(), relationship, n.ID); !errors.Is(err, ErrRecovery) {
		t.Fatal("unknown physical bytes adopted", err)
	}
	if err := os.Remove(foreign); err != nil {
		t.Fatal(err)
	}
	staging := filepath.Join(root, ".cluster-upload-legacy.part")
	if err := os.WriteFile(staging, []byte("interrupted legacy bytes"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := AdoptOwnedStorage(ctx, root, db.Maintenance(), db.Media(), db.Nodes(), relationship, n.ID); !errors.Is(err, ErrRecovery) {
		t.Fatal("legacy stage erased/ignored", err)
	}
	if err := os.Remove(staging); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Database().Exec("CREATE TRIGGER adoption_audit_failure BEFORE INSERT ON node_audit WHEN NEW.action='storage-owned-adopted' BEGIN SELECT RAISE(ABORT,'injected'); END"); err != nil {
		t.Fatal(err)
	}
	if _, err := AdoptOwnedStorage(ctx, root, db.Maintenance(), db.Media(), db.Nodes(), relationship, n.ID); err == nil {
		t.Fatal("unaudited adoption committed")
	}
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM cluster_upload_sessions WHERE state='complete'").Scan(&count); err != nil || count != 0 {
		t.Fatal("partial ledger escaped rollback", count, err)
	}
	if _, err := db.Database().Exec("DROP TRIGGER adoption_audit_failure"); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		adopted, err := AdoptOwnedStorage(ctx, root, db.Maintenance(), db.Media(), db.Nodes(), relationship, n.ID)
		want := 3
		if i == 1 {
			want = 0
		}
		if err != nil || adopted != want {
			t.Fatal(adopted, err)
		}
	}
	ownedFunds(t, db, 30, 0)
	for _, name := range paths {
		o, err := db.Media().ObjectByID(ctx, ids[name])
		if err != nil || o == nil || o.Path != name {
			t.Fatal("identity changed", o, err)
		}
		if data, err := os.ReadFile(filepath.Join(root, name)); err != nil || string(data) != "ID3payload" {
			t.Fatal("bytes changed", err)
		}
	}
	if err := svc.OwnedDelete(ctx, relationship, ids[paths[0]], paths[0], 10, store.NodeAudit{}); err != nil {
		t.Fatal("adopted object cannot drain", err)
	}
	ownedFunds(t, db, 20, 0)
}
