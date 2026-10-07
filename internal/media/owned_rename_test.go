package media

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestOwnedRenamePreservesAccountingAndReplaysLostAcknowledgement(t *testing.T) {
	root, db, svc, relationship := ownedFixture(t)
	svc.nodes = db.Nodes()
	ctx := context.Background()
	old, target, id := "music/OwnedRename%_!", "music/Renamed%_!", strings.Repeat("7", 32)
	object := store.MediaObject{ID: strings.Repeat("7", 64), Path: old + "/song.mp3", Kind: "audio"}
	if _, err := svc.OwnedUpload(ctx, relationship, object, 10, strings.NewReader("ID3payload"), store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Rename(ctx, old, filepath.Base(target), store.AdminAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("ordinary Follower rename allowed", err)
	}
	svc.owned = &lostOwnedRename{OwnedStorageRepository: db.Pool()}
	if _, err := svc.OwnedRename(ctx, relationship, old, target, id, store.NodeAudit{RequestID: "owned-rename"}); !errors.Is(err, ErrRecovery) {
		t.Fatal("lost commit", err)
	}
	if err := svc.Ready(ctx); !errors.Is(err, ErrRecovery) {
		t.Fatal("unresolved rename serves traffic", err)
	}
	if _, err := os.Stat(filepath.Join(root, target, "song.mp3")); err != nil {
		t.Fatal(err)
	}
	svc.Close()
	restarted, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer restarted.Close()
	for range 2 {
		if result, err := restarted.OwnedRename(ctx, relationship, old, target, id, store.NodeAudit{}); err != nil || result.Status != "renamed" {
			t.Fatal(result, err)
		}
	}
	actual, err := db.Media().ObjectByID(ctx, object.ID)
	if err != nil || actual == nil || actual.Path != target+"/song.mp3" {
		t.Fatal(actual, err)
	}
	var name string
	if err := db.Database().QueryRow("SELECT media_path FROM cluster_upload_sessions WHERE media_id=?", object.ID).Scan(&name); err != nil || name != actual.Path {
		t.Fatal("stale publication ledger", name, err)
	}
	ownedFunds(t, db, 10, 0)
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM node_audit WHERE action='storage-directory-renamed'").Scan(&count); err != nil || count != 1 {
		t.Fatal("replayed node audit", count, err)
	}
	if _, err := restarted.OwnedRename(ctx, relationship, target, "music/Another", id, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("operation rebound", err)
	}
	if err := restarted.OwnedDelete(ctx, relationship, object.ID, actual.Path, 10, store.NodeAudit{}); err != nil {
		t.Fatal("renamed file could not refund accounted bytes", err)
	}
	ownedFunds(t, db, 0, 0)
}

type lostOwnedRename struct{ store.OwnedStorageRepository }

func (r *lostOwnedRename) CompleteOwnedRename(ctx context.Context, rel, old, target, operation string, a store.NodeAudit) error {
	if err := r.OwnedStorageRepository.CompleteOwnedRename(ctx, rel, old, target, operation, a); err != nil {
		return err
	}
	return errors.New("lost rename commit acknowledgement")
}

func TestOwnedRenameAuditRollbackRecoversWithoutDoubleCharge(t *testing.T) {
	root, db, svc, relationship := ownedFixture(t)
	ctx := context.Background()
	old, target := "music/OwnedRollback", "music/RecoveredRollback"
	object := store.MediaObject{ID: strings.Repeat("8", 64), Path: old + "/song.mp3", Kind: "audio"}
	if _, err := svc.OwnedUpload(ctx, relationship, object, 10, strings.NewReader("ID3payload"), store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Database().Exec("CREATE TRIGGER rename_audit_fail BEFORE INSERT ON node_audit WHEN NEW.action='storage-directory-renamed' BEGIN SELECT RAISE(FAIL,'audit unavailable'); END"); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.OwnedRename(ctx, relationship, old, target, strings.Repeat("8", 32), store.NodeAudit{}); !errors.Is(err, ErrRecovery) {
		t.Fatal(err)
	}
	actual, err := db.Media().ObjectByID(ctx, object.ID)
	if err != nil || actual == nil || actual.Path != object.Path {
		t.Fatal("metadata not rolled back", actual, err)
	}
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='directory_rename'").Scan(&count); err != nil || count != 0 {
		t.Fatal("partial audit commit", count, err)
	}
	ownedFunds(t, db, 10, 0)
	if _, err := db.Database().Exec("DROP TRIGGER rename_audit_fail"); err != nil {
		t.Fatal(err)
	}
	svc.Close()
	restarted, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer restarted.Close()
	if _, err := restarted.OwnedRename(ctx, relationship, old, target, strings.Repeat("8", 32), store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	ownedFunds(t, db, 10, 0)
}

func TestOwnedRenameRejectsReservationsTargetsAndWrongRelationships(t *testing.T) {
	root, db, svc, relationship := ownedFixture(t)
	ctx := context.Background()
	old, target := "music/OwnedPreflight", "music/PreflightTarget"
	object := store.MediaObject{ID: strings.Repeat("9", 64), Path: old + "/song.mp3", Kind: "audio"}
	if _, err := svc.OwnedUpload(ctx, relationship, object, 10, strings.NewReader("ID3payload"), store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{old + "/pending.mp3", target + "/pending.mp3"} {
		reservation := store.MediaObject{ID: strings.Repeat("a", 64), Path: name, Kind: "audio"}
		if err := db.Pool().ReserveOwnedUpload(ctx, strings.Repeat("a", 32), relationship, reservation, 10, 5*store.GiB, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		if _, err := svc.OwnedRename(ctx, relationship, old, target, strings.Repeat("9", 32), store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
			t.Fatal("reserved upload renamed", err)
		}
		if err := db.Pool().ReleaseOwnedUpload(ctx, strings.Repeat("a", 32), store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := svc.OwnedRename(ctx, strings.Repeat("b", 32), old, target, strings.Repeat("9", 32), store.NodeAudit{}); err == nil {
		t.Fatal("unpaired relationship allowed")
	}
	for _, name := range []string{"vido/Elsewhere", "music/../escape", "music/.private", "music/a/deeper", "music/" + strings.Repeat("中", 86)} {
		if _, err := svc.OwnedRename(ctx, relationship, old, name, strings.Repeat("9", 32), store.NodeAudit{}); !errors.Is(err, ErrPath) {
			t.Fatal("unsafe rename allowed", name, err)
		}
	}
	if err := os.Mkdir(filepath.Join(root, target), 0750); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.OwnedRename(ctx, relationship, old, target, strings.Repeat("9", 32), store.NodeAudit{}); !errors.Is(err, os.ErrExist) {
		t.Fatal("occupied target moved", err)
	}
	if _, err := os.Stat(filepath.Join(root, object.Path)); err != nil {
		t.Fatal("preflight moved source", err)
	}
}
