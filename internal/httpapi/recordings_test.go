package httpapi

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/recording"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type recordingNode struct {
	router  *gin.Engine
	db      *sqlite.Store
	dir     string
	public  *Public
	control *node.Service
	volume  *recording.Storage
	id      string
}

func nativeRecordingFixture(t *testing.T, origin, role string, transport *clusterHTTP) recordingNode {
	t.Helper()
	r, db, dir, p, control := clusterFixture(t, origin, transport, true)
	root, e := os.OpenRoot(filepath.Join(dir, "recordings"))
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { root.Close() })
	volume, e := recording.New(root, db.Recordings(), db.Nodes())
	if e != nil {
		t.Fatal(e)
	}
	resolver, _ := network.New(nil)
	RegisterNodeRecordings(r, p.settings, resolver, control, volume)
	allocation := int64(0)
	if role == "Master" {
		allocation = 5 * store.GiB
	}
	identity, e := control.Promote(context.Background(), role, origin, allocation, store.NodeAudit{})
	if e != nil {
		t.Fatal(e)
	}
	return recordingNode{r, db, dir, p, control, volume, identity.ID}
}
func TestRecordingClusterPrimaryDirectRelayRangeFinalizeOfflineDeleteAndTokenReplay(t *testing.T) {
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	master := nativeRecordingFixture(t, "https://record-master.test", "Master", transport)
	direct := nativeRecordingFixture(t, "https://record-direct.test", "Follower", transport)
	relay := nativeRecordingFixture(t, "https://record-relay.test", "Follower", transport)
	for i, f := range []*recordingNode{&direct, &relay} {
		pack, e := f.control.CreatePair(ctx, store.NodeAudit{})
		if e != nil {
			t.Fatal(e)
		}
		relation, e := master.control.ImportPair(ctx, pack, store.NodeAudit{})
		if e != nil {
			t.Fatal(e)
		}
		cfg := store.ResourceConfiguration{}
		cfg.Storage.Enabled = true
		cfg.Storage.Allocation = 5 * store.GiB
		if e = master.db.Pool().ConfigureMember(ctx, f.id, cfg, store.NodeAudit{}); e != nil {
			t.Fatal(e)
		}
		mode := "Direct"
		if i == 1 {
			mode = "Relay"
		}
		if e = master.db.Nodes().SetRelationshipMode(ctx, relation, mode, false, store.NodeAudit{}); e != nil {
			t.Fatal(e)
		}
		rel, e := master.db.Nodes().Relationship(ctx, relation)
		if e != nil {
			t.Fatal(e)
		}
		if e = master.control.Tick(ctx, rel); e != nil {
			t.Fatal(e)
		}
	}
	u := store.KaraokeUser{ID: strings.Repeat("b", 32), Username: "录音账号", NameKey: "录音账号", PasswordHash: "fixture", Quota: 1024 * 1024}
	if e := master.db.Karaoke().RegisterUser(ctx, u, "192.0.2.180", "20261001", store.KaraokeAudit{}); e != nil {
		t.Fatal(e)
	}
	manager := recording.NewManager(master.db.Recordings(), master.db.Karaoke(), master.db.Nodes(), master.db.Pool(), master.control, master.volume)
	metadata := []byte(`{"version":1,"title":"尾部现场","lyrics":[{"time":1.25,"text":"中英歌词snow"}]}`)
	data := append([]byte("opaque recording payload"), metadata...)
	var length [8]byte
	binary.BigEndian.PutUint64(length[:], uint64(len(metadata)))
	data = append(data, length[:]...)
	data = append(data, []byte(recording.TrailerMagic)...)
	for _, site := range []struct {
		name, owner, origin string
		follower            *recordingNode
	}{{"primary", master.id, "https://record-master.test", nil}, {"direct", direct.id, "https://record-direct.test", &direct}, {"relay", relay.id, "https://record-relay.test", &relay}} {
		t.Run(site.name, func(t *testing.T) {
			if _, e := master.db.Database().Exec("UPDATE cluster_storage_members SET writable=CASE WHEN member_id=? THEN 1 ELSE 0 END", site.owner); e != nil {
				t.Fatal(e)
			}
			ticket, e := manager.Ticket(ctx, u, int64(len(data)), "audio/webm", store.RecordingMetadata{Title: "起始标题", Lyrics: []store.RecordingLyric{}}, store.KaraokeAudit{})
			if e != nil {
				t.Fatal(e)
			}
			row, e := master.db.Recordings().Recording(ctx, ticket.ID)
			if e != nil || row.MemberID != site.owner {
				t.Fatal(row, e)
			}
			if site.name == "direct" {
				if !ticket.Direct || ticket.Capability == nil {
					t.Fatal(ticket)
				}
				for range 2 {
					r := httptest.NewRequest("PUT", ticket.URL, bytes.NewReader(data))
					r.Header.Set("X-Recording-Capability", *ticket.Capability)
					r.Header.Set("Origin", "https://record-master.test")
					w := httptest.NewRecorder()
					site.follower.router.ServeHTTP(w, r)
					if w.Code != 200 {
						t.Fatal("direct upload", w.Code, w.Body.String())
					}
				}
			} else {
				receipt, e := manager.Upload(ctx, u.ID, ticket.ID, bytes.NewReader(data), store.KaraokeAudit{})
				if e != nil || receipt.Bytes != int64(len(data)) {
					t.Fatal(receipt, e)
				}
			}
			for range 2 {
				if e := manager.Finalize(ctx, u.ID, ticket.ID, store.KaraokeAudit{}); e != nil {
					t.Fatal(e)
				}
			}
			items, e := manager.List(ctx, u.ID)
			if e != nil || len(items) != 1 || items[0].Title != "尾部现场" || len(items[0].Lyrics) != 1 {
				t.Fatal(items, e)
			}
			p, target, e := manager.Delivery(ctx, u.ID, ticket.ID, false, true)
			if e != nil {
				t.Fatal(e)
			}
			if site.name == "primary" {
				if !p.Local || target != "" {
					t.Fatal(p, target)
				}
				file, _, release, e := master.volume.Open(ctx, master.id, u.ID, ticket.ID)
				if e != nil {
					t.Fatal(e)
				}
				file.Close()
				release()
			} else if site.name == "direct" {
				for _, method := range []string{"GET", "HEAD"} {
					r := httptest.NewRequest(method, target, nil)
					r.Header.Set("Origin", "https://record-master.test")
					r.Header.Set("Range", "bytes=1-4")
					w := httptest.NewRecorder()
					direct.router.ServeHTTP(w, r)
					if w.Code != 206 || method == "GET" && w.Body.String() != string(data[1:5]) || method == "HEAD" && w.Body.Len() != 0 {
						t.Fatal("range/HEAD", method, w.Code, w.Body.String())
					}
				}
				r := httptest.NewRequest("GET", target, nil)
				r.Header.Set("Origin", "https://evil.test")
				w := httptest.NewRecorder()
				direct.router.ServeHTTP(w, r)
				if w.Code != 403 {
					t.Fatal("unpaired origin", w.Code)
				}
			} else if !strings.HasPrefix(target, "/_relay_recording/record-relay.test/443/") {
				t.Fatal("relay delivery", target)
			}
			if site.follower != nil {
				delete(transport.routers, site.origin)
				if e = manager.Delete(ctx, u.ID, ticket.ID, false, store.KaraokeAudit{}); e == nil {
					t.Fatal("offline deletion refunded")
				}
				pending, e := master.db.Recordings().Recording(ctx, ticket.ID)
				if e != nil || pending.State != "deleting" {
					t.Fatal(pending, e)
				}
				user, e := master.db.Karaoke().UserByID(ctx, u.ID)
				if e != nil || user.Used != int64(len(data)) {
					t.Fatal("uncertain refund", user, e)
				}
				transport.routers[site.origin] = site.follower.router
			}
			for range 2 {
				if e = manager.Delete(ctx, u.ID, ticket.ID, false, store.KaraokeAudit{}); e != nil {
					t.Fatal(e)
				}
			}
			user, e := master.db.Karaoke().UserByID(ctx, u.ID)
			if e != nil || user.Used != 0 {
				t.Fatal("quota deletion replay", user, e)
			}
			if site.follower != nil {
				tombstone, e := site.follower.db.Recordings().Recording(ctx, ticket.ID)
				if e != nil || tombstone.State != "deleted" {
					t.Fatal(tombstone, e)
				}
				if site.name == "direct" {
					r := httptest.NewRequest("PUT", ticket.URL, bytes.NewReader(data))
					r.Header.Set("X-Recording-Capability", *ticket.Capability)
					w := httptest.NewRecorder()
					direct.router.ServeHTTP(w, r)
					if w.Code != 409 {
						t.Fatal("old upload token resurrected deleted recording", w.Code, w.Body.String())
					}
				}
				members, e := site.follower.db.Pool().Members(ctx)
				if e != nil || len(members) != 1 || members[0].Reserved != 0 || members[0].Used != 0 {
					t.Fatal("follower accounting", members, e)
				}
			}
		})
	}
	// Cancelling a never-uploaded recording still needs an absent-path proof.
	if _, e := master.db.Database().Exec("UPDATE cluster_storage_members SET writable=CASE WHEN member_id=? THEN 1 ELSE 0 END", direct.id); e != nil {
		t.Fatal(e)
	}
	unused, e := manager.Ticket(ctx, u, 100, "audio/webm", store.RecordingMetadata{}, store.KaraokeAudit{})
	if e != nil {
		t.Fatal(e)
	}
	if e = manager.Delete(ctx, u.ID, unused.ID, true, store.KaraokeAudit{}); e != nil {
		t.Fatal(e)
	}
	r := httptest.NewRequest("PUT", unused.URL, strings.NewReader(strings.Repeat("x", 100)))
	r.Header.Set("X-Recording-Capability", *unused.Capability)
	w := httptest.NewRecorder()
	direct.router.ServeHTTP(w, r)
	if w.Code != 409 {
		t.Fatal("never-uploaded cancelled ticket revived", w.Code, w.Body.String())
	}
	oldOwnerTicket, e := manager.Ticket(ctx, u, 100, "audio/webm", store.RecordingMetadata{}, store.KaraokeAudit{})
	if e != nil {
		t.Fatal(e)
	}
	_, _, e = manager.Delivery(ctx, u.ID, unused.ID, false, true)
	if !errors.Is(e, store.ErrRecordingMissing) {
		t.Fatal("deleted recording delivered", e)
	}
	members, e := master.db.Pool().Members(ctx)
	if e != nil {
		t.Fatal(e)
	}
	var directMember store.StorageMember
	for _, m := range members {
		if m.ID == direct.id {
			directMember = m
		}
	}
	rel, e := master.db.Nodes().Relationship(ctx, *directMember.RelationshipID)
	if e != nil {
		t.Fatal(e)
	}
	result, e := master.control.Call(ctx, rel, "/internal/v1/recordings/users/"+u.ID+"/delete", map[string]any{})
	if e != nil || result["status"] != "deleted" {
		t.Fatal("owned user cleanup", result, e)
	}
	r = httptest.NewRequest("PUT", oldOwnerTicket.URL, strings.NewReader(strings.Repeat("x", 100)))
	r.Header.Set("X-Recording-Capability", *oldOwnerTicket.Capability)
	w = httptest.NewRecorder()
	direct.router.ServeHTTP(w, r)
	if w.Code != 409 {
		t.Fatal("deleted owner accepted old in-flight ticket", w.Code, w.Body.String())
	}
	if e = manager.Delete(ctx, u.ID, oldOwnerTicket.ID, true, store.KaraokeAudit{}); e != nil {
		t.Fatal(e)
	}
	if _, e := master.db.Database().Exec("UPDATE cluster_storage_members SET writable=CASE WHEN member_id=? THEN 1 ELSE 0 END", master.id); e != nil {
		t.Fatal(e)
	}
	ticket, e := manager.Ticket(ctx, u, 100, "audio/webm", store.RecordingMetadata{}, store.KaraokeAudit{})
	if e != nil {
		t.Fatal(e)
	}
	if e = manager.Delete(ctx, u.ID, ticket.ID, true, store.KaraokeAudit{}); e != nil {
		t.Fatal(e)
	}
	if _, e = manager.PendingUpload(ctx, u.ID, ticket.ID); !errors.Is(e, store.ErrRecordingMissing) {
		t.Fatal(e)
	}
}
