package httpapi

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestGlobalRenameMixedOwnersOfflineRestartAndLegacyLostReplyConverge(t *testing.T) {
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	master := nativeRecordingFixture(t, "https://global-rename-master.test", "Master", transport)
	direct := nativeRecordingFixture(t, "https://global-rename-direct.test", "Follower", transport)
	relay := nativeRecordingFixture(t, "https://global-rename-relay.test", "Follower", transport)
	relations := map[string]store.Relationship{}
	for i, f := range []*recordingNode{&direct, &relay} {
		pack, err := f.control.CreatePair(ctx, store.NodeAudit{})
		if err != nil {
			t.Fatal(err)
		}
		id, err := master.control.ImportPair(ctx, pack, store.NodeAudit{})
		if err != nil {
			t.Fatal(err)
		}
		cfg := store.ResourceConfiguration{}
		cfg.Storage.Enabled, cfg.Storage.Allocation = true, 5*store.GiB
		if err := master.db.Pool().ConfigureMember(ctx, f.id, cfg, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		mode := "Direct"
		if i == 1 {
			mode = "Relay"
		}
		if err := master.db.Nodes().SetRelationshipMode(ctx, id, mode, false, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		rel, err := master.db.Nodes().Relationship(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		if err := master.control.Tick(ctx, rel); err != nil {
			t.Fatal(err)
		}
		relations[f.id] = rel
	}
	old, target := "music/混合目录%_!", "music/已改名目录%_!"
	tickets := []media.UploadTicket{}
	for _, site := range []string{"primary", "direct", "relay"} {
		ticket, err := master.public.media.ReserveMasterUpload(ctx, site+".mp3", old+"/"+site, "", site, 10, 255, store.AdminAudit{})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := master.public.media.UploadMasterBytes(ctx, ticket.ID, strings.NewReader("ID3payload"), store.AdminAudit{}); err != nil {
			t.Fatal(err)
		}
		tickets = append(tickets, ticket)
	}
	op, err := master.db.Pool().PrepareGlobalRename(ctx, old, target, store.AdminAudit{RequestID: "mixed-rename"})
	if err != nil || len(op.Media) != 3 {
		t.Fatal(op, err)
	}
	// Simulate a legacy Follower move whose HTTP acknowledgement was lost. Its
	// private operation ID differs from the native Master intent's ID.
	if _, err := master.control.Call(ctx, relations[direct.id], "/internal/v1/storage-control/directory-rename", map[string]any{"old_path": old, "new_path": target}); err != nil {
		t.Fatal(err)
	}
	origin := relations[direct.id].Endpoint
	savedRouter := transport.routers[origin]
	delete(transport.routers, origin)
	if err := master.public.media.RetryGlobalRenames(ctx); err != nil {
		t.Fatal(err)
	}
	current, err := master.db.Pool().GlobalRename(ctx, op.ID)
	if err != nil || current == nil || current.State != "rename_pending" {
		t.Fatal("offline owner falsely completed", current, err)
	}
	if rows, err := master.db.Pool().Resources(ctx, "music", false); err != nil || len(rows) != 0 {
		t.Fatal("partial rename exposed", rows, err)
	}
	for _, name := range []string{old, target} {
		if _, err := master.public.media.ReserveMasterUpload(ctx, "blocked.mp3", name, "", "primary", 10, 255, store.AdminAudit{}); err == nil {
			t.Fatal("pending scope reused", name)
		}
	}
	master.public.media.Close()
	restarted, err := media.New(filepath.Join(master.dir, "media"), master.db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer restarted.Close()
	restarted.ConfigureCluster(master.db.Nodes(), master.db.Pool(), master.control)
	transport.routers[origin] = savedRouter
	if err := restarted.RetryGlobalRenames(ctx); err != nil {
		t.Fatal(err)
	}
	current, err = master.db.Pool().GlobalRename(ctx, op.ID)
	if err != nil || current == nil || current.State != "rename_done" {
		t.Fatal("rename did not converge", current, err)
	}
	for _, ticket := range tickets {
		actual, err := master.db.Pool().Resource(ctx, ticket.MediaID)
		if err != nil || actual.Path != target+strings.TrimPrefix(ticket.Path, old) || actual.MemberID != ticket.MemberID || actual.ObjectID != ticket.MediaID {
			t.Fatal("placement changed", actual, err)
		}
		owner := &master
		if ticket.Site == "direct" {
			owner = &direct
		} else if ticket.Site == "relay" {
			owner = &relay
		}
		if _, err := os.Stat(filepath.Join(owner.dir, "media", actual.Path)); err != nil {
			t.Fatal("physical destination absent", actual.Path, err)
		}
		var used, reserved int64
		if err := master.db.Database().QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", ticket.MemberID).Scan(&used, &reserved); err != nil || used != 10 || reserved != 0 {
			t.Fatal("rename changed accounting", used, reserved, err)
		}
	}
	result, err := restarted.DeleteGlobal(ctx, []string{target}, store.AdminAudit{})
	if err != nil || result.Deleted != 3 || len(result.Pending) != 0 {
		t.Fatal("renamed mixed directory deletion", result, err)
	}
	for _, n := range []*recordingNode{&master, &direct, &relay} {
		var used, reserved int64
		if err := n.db.Database().QueryRow("SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=?", n.id).Scan(&used, &reserved); err != nil || used != 0 || reserved != 0 {
			t.Fatal("renamed node quota not refunded", used, reserved, err)
		}
	}
}
