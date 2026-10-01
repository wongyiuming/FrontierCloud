package media

import (
	"context"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type lostGlobalRenameCommit struct {
	store.PoolRepository
	cancel context.CancelFunc
}

func (r *lostGlobalRenameCommit) CompleteGlobalRename(ctx context.Context, id string) error {
	if err := r.PoolRepository.CompleteGlobalRename(ctx, id); err != nil {
		return err
	}
	r.cancel()
	return errors.New("lost SQL commit acknowledgement")
}

func TestGlobalRenameCommittedCleanupResumesAfterCancelledReply(t *testing.T) {
	_, db, svc := masterFixture(t)
	ctx := context.Background()
	request, cancel := context.WithCancel(ctx)
	defer cancel()
	svc.pool = &lostGlobalRenameCommit{PoolRepository: db.Pool(), cancel: cancel}
	if _, err := svc.Rename(request, "music/artist", "CommittedArtist", store.AdminAudit{RequestID: "global-rename-lost-commit"}); err == nil {
		t.Fatal("cancelled uncertain reply unexpectedly succeeded")
	}
	operations, err := db.Pool().PendingGlobalRenames(ctx, 10)
	if err != nil || len(operations) != 1 || operations[0].State != "rename_cleanup" {
		t.Fatal("committed cleanup intent lost", operations, err)
	}
	svc.pool = db.Pool()
	if err := svc.RetryGlobalRenames(ctx); err != nil {
		t.Fatal(err)
	}
	current, err := db.Pool().GlobalRename(ctx, operations[0].ID)
	if err != nil || current == nil || current.State != "rename_done" {
		t.Fatal(current, err)
	}
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='directory_rename' AND request_id='global-rename-lost-commit'").Scan(&count); err != nil || count != 1 {
		t.Fatal("commit replayed audit", count, err)
	}
	masterFunds(t, db, 0, 0)
}

type pausedRoleRead struct {
	store.NodeRepository
	started chan struct{}
	resume  chan struct{}
	first   bool
}

func (r *pausedRoleRead) ReadIdentity(ctx context.Context) (store.NodeIdentity, error) {
	row, err := r.NodeRepository.ReadIdentity(ctx)
	if r.first {
		r.first = false
		close(r.started)
		select {
		case <-r.resume:
		case <-ctx.Done():
			return row, ctx.Err()
		}
	}
	return row, err
}

func TestStandaloneRenameCannotBypassConcurrentMasterPromotion(t *testing.T) {
	_, db, svc := deleteFixture(t)
	ctx := context.Background()
	identity, err := node.Initialize(ctx, db.Nodes(), filepath.Join(t.TempDir(), "secrets"))
	if err != nil {
		t.Fatal(err)
	}
	svc.identity = identity
	paused := &pausedRoleRead{NodeRepository: db.Nodes(), started: make(chan struct{}), resume: make(chan struct{}), first: true}
	svc.ConfigureCluster(paused, db.Pool(), nil)
	result := make(chan error, 1)
	go func() { _, err := svc.Rename(ctx, "music/artist", "BypassArtist", store.AdminAudit{}); result <- err }()
	<-paused.started
	if err := svc.WithPromotion(ctx, "Master", func(p store.NodePromotion) error {
		p.Endpoint, p.Allocation = "https://promoted.test", 5*store.GiB
		_, err := db.Nodes().PromoteIdentity(ctx, p, store.NodeAudit{})
		return err
	}); err != nil {
		close(paused.resume)
		t.Fatal(err)
	}
	close(paused.resume)
	if err := <-result; !errors.Is(err, store.ErrNodeState) {
		t.Fatal("Standalone request bypassed Master coordinator", err)
	}
	rows, err := db.Pool().Resources(ctx, "music/artist", false)
	if err != nil || len(rows) != 1 || !strings.HasPrefix(rows[0].Path, "music/artist/") {
		t.Fatal("promotion catalog corrupted", rows, err)
	}
}

func TestGlobalRenameLocalSQLRollbackRollsForwardAfterRestart(t *testing.T) {
	root, db, svc := masterFixture(t)
	ctx := context.Background()
	rows, err := db.Pool().Resources(ctx, "music/artist", false)
	if err != nil || len(rows) != 1 {
		t.Fatal(rows, err)
	}
	id := rows[0].ID
	if _, err := db.Pool().RecordGlobalPlayback(ctx, id, "20512c3b-5340-4185-b76d-20402279482a"); err != nil {
		t.Fatal(err)
	}
	if err := db.Media().SetHidden(ctx, []string{"music/artist"}, true, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Database().Exec("CREATE TRIGGER global_rename_audit_fail BEFORE INSERT ON admin_audit_log WHEN NEW.action='directory_rename' BEGIN SELECT RAISE(FAIL,'audit unavailable'); END"); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Rename(ctx, "music/artist", "RenamedArtist", store.AdminAudit{RequestID: "global-rename-recovery"}); !errors.Is(err, ErrUnavailable) {
		t.Fatal("partial move reported success", err)
	}
	if _, err := os.Stat(filepath.Join(root, "music/RenamedArtist/song.mp3")); err != nil {
		t.Fatal(err)
	}
	local, err := db.Media().ObjectByID(ctx, id)
	if err != nil || local == nil || local.Path != "music/artist/song.mp3" {
		t.Fatal("SQL did not roll back", local, err)
	}
	if rows, err := db.Pool().Resources(ctx, "music", false); err != nil || len(rows) != 1 || rows[0].Path != "music/other/song.mp3" {
		t.Fatal("partial path catalog exposed", rows, err)
	}
	operations, err := db.Pool().PendingGlobalRenames(ctx, 10)
	if err != nil || len(operations) != 1 {
		t.Fatal(operations, err)
	}
	if _, err := db.Database().Exec("DROP TRIGGER global_rename_audit_fail"); err != nil {
		t.Fatal(err)
	}
	svc.Close()
	restarted, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer restarted.Close()
	restarted.ConfigureCluster(db.Nodes(), db.Pool(), nil)
	if err := restarted.RetryGlobalRenames(ctx); err != nil {
		t.Fatal(err)
	}
	op, err := db.Pool().GlobalRename(ctx, operations[0].ID)
	if err != nil || op == nil || op.State != "rename_done" {
		t.Fatal(op, err)
	}
	global, err := db.Pool().Resource(ctx, id)
	if err != nil || global.Path != "music/RenamedArtist/song.mp3" || global.PlayScore != 1 {
		t.Fatal(global, err)
	}
	local, err = db.Media().ObjectByID(ctx, id)
	if err != nil || local == nil || local.Path != global.Path {
		t.Fatal(local, err)
	}
	hidden, err := db.Media().HiddenPaths(ctx)
	if err != nil || hidden["music/artist"] || !hidden["music/RenamedArtist"] {
		t.Fatal(hidden, err)
	}
	masterFunds(t, db, 0, 0)
	if _, err := os.Stat(filepath.Join(root, "music/RenamedArtist", globalRenameMarker(op.ID))); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("marker not cleaned", err)
	}
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE action='directory_rename' AND request_id='global-rename-recovery'").Scan(&count); err != nil || count != 1 {
		t.Fatal("audit not exact once", count, err)
	}
	if result, err := restarted.DeleteGlobal(ctx, []string{"music/RenamedArtist"}, store.AdminAudit{}); err != nil || result.Deleted != 1 || len(result.Pending) != 0 {
		t.Fatal("renamed MasterLocal deletion", result, err)
	}
	masterFunds(t, db, -10, 0)
}

func TestGlobalRenameNeverAdoptsUnownedDestination(t *testing.T) {
	root, db, svc := masterFixture(t)
	ctx := context.Background()
	op, err := db.Pool().PrepareGlobalRename(ctx, "music/artist", "music/UnownedTarget", store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	// An external move without this operation's durable ownership marker is not
	// accepted as a recovery proof, even when the directory/file names match.
	if err := os.Rename(filepath.Join(root, op.Old), filepath.Join(root, op.New)); err != nil {
		t.Fatal(err)
	}
	if err := svc.retryGlobalRename(ctx, op); !errors.Is(err, ErrRecovery) {
		t.Fatal("unowned destination accepted", err)
	}
	current, err := db.Pool().GlobalRename(ctx, op.ID)
	if err != nil || current == nil || current.State != "rename_pending" {
		t.Fatal(current, err)
	}
	if _, err := os.Stat(filepath.Join(root, op.New, "song.mp3")); err != nil {
		t.Fatal("unowned destination altered", err)
	}
	masterFunds(t, db, 0, 0)
}

func TestGlobalRenameRepairsOnlyInterruptedOwnedSourceMarker(t *testing.T) {
	for _, valid := range []bool{true, false} {
		t.Run(map[bool]string{true: "owned-prefix", false: "foreign-content"}[valid], func(t *testing.T) {
			root, db, svc := masterFixture(t)
			ctx := context.Background()
			op, err := db.Pool().PrepareGlobalRename(ctx, "music/artist", "music/MarkerRecovery", store.AdminAudit{})
			if err != nil {
				t.Fatal(err)
			}
			data := []byte(op.ID[:8])
			if !valid {
				data = []byte("unknown")
			}
			if err := os.WriteFile(filepath.Join(root, op.Old, globalRenameMarker(op.ID)), data, 0600); err != nil {
				t.Fatal(err)
			}
			err = svc.retryGlobalRename(ctx, op)
			if !valid {
				if !errors.Is(err, ErrRecovery) {
					t.Fatal("foreign marker accepted", err)
				}
				if _, err := os.Stat(filepath.Join(root, op.Old, "song.mp3")); err != nil {
					t.Fatal("foreign source moved", err)
				}
				return
			}
			if err != nil {
				t.Fatal("interrupted source marker did not recover", err)
			}
			if _, err := os.Stat(filepath.Join(root, op.New, "song.mp3")); err != nil {
				t.Fatal(err)
			}
			masterFunds(t, db, 0, 0)
		})
	}
}
