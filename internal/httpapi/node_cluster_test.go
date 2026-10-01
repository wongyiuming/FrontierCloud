package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	storeSQLite "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

// HTTP routing, raw body HMAC and UseNumber decoding are real. The node
// transport socket/private CA/hostname checks have separate real TLS tests.
type clusterHTTP struct{ routers map[string]*gin.Engine }

func (t *clusterHTTP) StorageUpload(ctx context.Context, origin, id, token string, reader io.Reader, size int64) (map[string]any, error) {
	router := t.routers[origin]
	if router == nil {
		return nil, errors.New("offline")
	}
	r := httptest.NewRequest("PUT", origin+"/internal/v1/storage/"+id, reader).WithContext(ctx)
	r.Header.Set("X-Storage-Capability", token)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, r)
	if w.Code != 200 {
		return nil, fmt.Errorf("storage HTTP %d", w.Code)
	}
	var result map[string]any
	err := controlJSON(w.Body.Bytes(), &result)
	return result, err
}

func (t *clusterHTTP) Identity(ctx context.Context, origin, id, key, role string) (node.Peer, error) {
	router := t.routers[origin]
	if router == nil {
		return node.Peer{}, errors.New("offline")
	}
	w := request(router, "GET", origin+"/internal/v1/identity?challenge="+strings.Repeat("a", 32), "")
	var signed node.Envelope
	if w.Code != 200 || controlJSON(w.Body.Bytes(), &signed) != nil {
		return node.Peer{}, errors.New("identity unavailable")
	}
	text := func(k string) string { v, _ := signed.Payload[k].(string); return v }
	public := text("public_key")
	if protocol.Verify(public, signed.Payload, signed.Signature) != nil || signed.Payload["challenge"] != strings.Repeat("a", 32) || id != "" && text("node_id") != id || key != "" && public != key || role != "" && text("role") != role {
		return node.Peer{}, errors.New("identity mismatch")
	}
	return node.Peer{ID: text("node_id"), Endpoint: origin, PublicKey: public, Role: text("role"), AppVersion: text("app_version")}, nil
}
func (t *clusterHTTP) Request(ctx context.Context, origin, route, method string, value any, relationship, credential string) (map[string]any, error) {
	router := t.routers[origin]
	if router == nil {
		return nil, errors.New("offline")
	}
	var raw []byte
	var err error
	if value != nil {
		raw, err = protocol.Canonical(value)
		if err != nil {
			return nil, err
		}
	}
	r := httptest.NewRequest(method, origin+route, bytes.NewReader(raw)).WithContext(ctx)
	r.Header.Set("Content-Type", "application/json")
	if relationship != "" {
		headers, err := protocol.AuthHeaders(credential, relationship, method, route, raw, time.Now().Unix())
		if err != nil {
			return nil, err
		}
		for k, v := range headers {
			r.Header.Set(k, v)
		}
	}
	w := httptest.NewRecorder()
	router.ServeHTTP(w, r)
	if w.Code != 200 {
		return nil, fmt.Errorf("control status %d: %s", w.Code, w.Body.String())
	}
	var result map[string]any
	err = controlJSON(w.Body.Bytes(), &result)
	return result, err
}

func clusterFixture(t *testing.T, origin string, transport *clusterHTTP, empty bool) (*gin.Engine, *storeSQLite.Store, string, *Public, *node.Service) {
	t.Helper()
	router, db, dir, public := publicFixture(t, false)
	if empty {
		for _, name := range []string{"music/artist/song.mp3", "music/nested/album/音乐.mp3", "vido/director/video.mp4"} {
			if err := os.Remove(filepath.Join(dir, "media", name)); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := os.Mkdir(filepath.Join(dir, "recordings"), 0700); err != nil {
		t.Fatal(err)
	}
	recordingRoot, err := os.OpenRoot(filepath.Join(dir, "recordings"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { recordingRoot.Close() })
	identity, err := node.Initialize(context.Background(), db.Nodes(), filepath.Join(dir, "secrets"))
	if err != nil {
		t.Fatal(err)
	}
	service := node.NewService(db.Nodes(), identity, transport)
	service.ConfigureVolumes(db.Pool(), public.media, recordingRoot)
	public.media.ConfigureCluster(db.Nodes(), db.Pool(), service)
	resolver, _ := network.New(nil)
	public.settings.TLSEnabled = true
	RegisterNodeIdentity(router, public.settings, resolver, service)
	RegisterNodeControl(router, public.settings, resolver, service)
	RegisterNodeMedia(router, public.settings, resolver, service, public.media)
	RegisterNodeStorage(router, public.settings, resolver, service, public.media)
	transport.routers[origin] = router
	return router, db, dir, public, service
}

func TestClusterHTTPPairCatalogDirectRelayCapabilitiesAndGlobalStats(t *testing.T) {
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	masterRouter, masterDB, _, masterPublic, master := clusterFixture(t, "https://master.test", transport, false)
	followerRouter, followerDB, _, _, follower := clusterFixture(t, "https://follower.test", transport, true)
	for _, v := range []struct {
		service      *node.Service
		role, origin string
		allocation   int64
	}{{master, "Master", "https://master.test", 10 * store.GiB}, {follower, "Follower", "https://follower.test", 0}} {
		if _, err := v.service.Promote(ctx, v.role, v.origin, v.allocation, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
	}
	pair, err := follower.CreatePair(ctx, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	relationID, err := master.ImportPair(ctx, pair, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = master.ImportPair(ctx, pair, store.NodeAudit{}); err == nil {
		t.Fatal("pair package reused")
	}
	relation, err := masterDB.Nodes().Relationship(ctx, relationID)
	if err != nil {
		t.Fatal(err)
	}
	if relation.Status != "offline" {
		t.Fatal("inbound pairing incorrectly established reachability")
	}
	cfg := store.ResourceConfiguration{}
	cfg.Storage.Enabled = true
	cfg.Storage.Allocation = 5 * store.GiB
	if err = masterDB.Pool().ConfigureMember(ctx, relation.PeerID, cfg, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err = masterDB.Nodes().SetRelationshipMode(ctx, relationID, "Direct", false, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	relation, _ = masterDB.Nodes().Relationship(ctx, relationID)
	if err = master.Tick(ctx, relation); err != nil {
		t.Fatal(err)
	}
	path := "music/远端专辑/曲目.mp3"
	upload, err := masterDB.Pool().ReserveUpload(ctx, path, "direct", 10, 20*store.GiB, store.AdminAudit{})
	if err != nil {
		t.Fatal(err)
	}
	storageToken, err := master.StorageCapability(ctx, upload, "upload")
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		r := httptest.NewRequest("PUT", "https://follower.test/internal/v1/storage/"+upload.MediaID, strings.NewReader("ID33456789"))
		r.Header.Set("X-Storage-Capability", storageToken)
		r.Header.Set("Origin", "https://master.test")
		w := httptest.NewRecorder()
		followerRouter.ServeHTTP(w, r)
		if w.Code != 200 {
			t.Fatal("durable Follower upload", w.Code, w.Body.String())
		}
	}
	stat, err := master.Call(ctx, relation, "/internal/v1/storage/"+upload.MediaID+"/stat", map[string]any{"path": path})
	if err != nil {
		t.Fatal(err)
	}
	etag, _ := stat["etag"].(string)
	if _, err = masterDB.Pool().FinalizeUpload(ctx, upload.ID, upload.MediaID, 10, etag, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	catalogTarget := "/api/v1/media/catalog/media?" + url.Values{"media_type": {"music"}, "path": {"music/远端专辑"}, "playback_session_id": {"test"}}.Encode()
	w := request(masterRouter, "GET", catalogTarget, "")
	var catalog struct{ Entries []media.Track }
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &catalog) != nil || len(catalog.Entries) != 1 || catalog.Entries[0].MediaID != upload.MediaID || catalog.Entries[0].ResourceID != upload.MediaID || catalog.Entries[0].KaraokeID == "" {
		t.Fatal("global virtual catalog", w.Code, w.Body.String())
	}
	w = request(masterRouter, "GET", "/api/v1/media/catalog/categories?media_type=music", "")
	if w.Code != 200 || !strings.Contains(w.Body.String(), "远端专辑") {
		t.Fatal("virtual categories", w.Body.String())
	}
	w = request(masterRouter, "GET", "/api/v1/media/music/category?"+url.Values{"path": {"music/远端专辑"}}.Encode(), "")
	if w.Code != 200 || strings.Contains(w.Body.String(), "{{") {
		t.Fatal("virtual player", w.Code, w.Body.String())
	}
	target := catalog.Entries[0].URL
	w = request(masterRouter, "GET", target, "")
	if w.Code != 307 || w.Header().Get("X-Media-Resource-ID") != upload.MediaID || w.Header().Get("Referrer-Policy") != "no-referrer" {
		t.Fatal("Direct delivery", w.Code, w.Body.String())
	}
	location := w.Header().Get("Location")
	parsed, _ := url.Parse(location)
	token := parsed.Query().Get("token")
	for _, v := range []struct {
		method, origin, rangeValue string
		want                       int
	}{{"GET", "https://master.test", "bytes=2-5", 206}, {"HEAD", "https://master.test", "", 200}, {"OPTIONS", "https://master.test", "", 200}, {"GET", "https://evil.test", "", 403}} {
		r := httptest.NewRequest(v.method, location, nil)
		r.Header.Set("Origin", v.origin)
		if v.rangeValue != "" {
			r.Header.Set("Range", v.rangeValue)
		}
		out := httptest.NewRecorder()
		followerRouter.ServeHTTP(out, r)
		if out.Code != v.want {
			t.Fatalf("owned media %+v: %d %s", v, out.Code, out.Body.String())
		}
		if v.want == 206 && out.Body.String() != "3345" {
			t.Fatal("incorrect Range")
		}
		if v.want == 200 && v.method != "GET" && out.Body.Len() != 0 {
			t.Fatal("HEAD/OPTIONS streamed body")
		}
	}
	r := httptest.NewRequest("GET", location, nil)
	r.Header.Set("X-Media-Capability", token+"x")
	out := httptest.NewRecorder()
	followerRouter.ServeHTTP(out, r)
	if out.Code != 401 {
		t.Fatal("conflicting capability", out.Code)
	}
	w = request(followerRouter, "GET", "http://follower.test"+parsed.RequestURI(), "")
	if w.Code != 403 {
		t.Fatal("plaintext capability accepted")
	}
	w = request(followerRouter, "GET", "/api/v1/media/stream?"+url.Values{"file_path": {path}}.Encode(), "")
	if w.Code != 404 {
		t.Fatal("Follower public catalog leaked placement")
	}
	for range 2 {
		body, _ := json.Marshal(map[string]any{"media_path": path, "resource_id": upload.MediaID, "playback_session_id": "20512c3b-5340-4185-b76d-20402279482a", "played_seconds": 30, "duration": 100})
		w = request(masterRouter, "POST", "/api/v1/media/playback", string(body))
		if w.Code != 200 {
			t.Fatal("global playback", w.Code, w.Body.String())
		}
	}
	stats, err := masterDB.Media().Stats(ctx, []string{upload.MediaID})
	if err != nil || stats[upload.MediaID].PlayScore != 1 {
		t.Fatal("global exact once", stats, err)
	}
	w = request(masterRouter, "GET", "/api/v1/media/lyrics/content?"+url.Values{"track": {path}, "resource_id": {upload.MediaID}}.Encode(), "")
	if w.Code != 200 || !strings.Contains(w.Body.String(), "暂无歌词") {
		t.Fatal("global fallback lyric", w.Code, w.Body.String())
	}
	volume := masterPublic.media
	tree, err := volume.Tree(ctx, "music/远端专辑")
	if err != nil || len(tree.Items) != 1 || tree.Items[0].MediaID != upload.MediaID || tree.Items[0].MemberID != upload.MemberID || tree.Items[0].NodeHealth != "online" {
		t.Fatal("global Admin tree", tree, err)
	}
	searched, err := volume.Search(ctx, "qumu", "music")
	if err != nil || len(searched.Items) != 1 || searched.Items[0].MediaID != upload.MediaID {
		t.Fatal("global pinyin search", searched, err)
	}
	if err = volume.Hide(ctx, []string{"music/远端专辑"}, true, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	w = request(masterRouter, "GET", catalogTarget, "")
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"entries":[]`) {
		t.Fatal("remote inherited hide", w.Body.String())
	}
	if err = volume.Hide(ctx, []string{"music/远端专辑"}, false, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err = volume.Preference(ctx, path, 42, false, store.AdminAudit{}); err != nil {
		t.Fatal("remote media priority", err)
	}
	if _, err = volume.Preference(ctx, "music/远端专辑", 30, true, store.AdminAudit{}); err != nil {
		t.Fatal("remote directory priority", err)
	}
	priorities, err := volume.Priorities(ctx, "music/远端专辑", "", "audio", 1, 100)
	if err != nil || len(priorities.Items) != 1 || priorities.Items[0].MediaID != upload.MediaID || priorities.Items[0].Preference != 42 {
		t.Fatal("global priority catalog", priorities, err)
	}
	stage, err := volume.Stage(ctx, strings.NewReader("[00:01]远端歌词\n"), 1024)
	if err != nil {
		t.Fatal(err)
	}
	lyricPath, err := volume.Publish(ctx, stage, "曲目.lrc", "", "", true, 240, store.AdminAudit{})
	stage.Close()
	if err != nil {
		t.Fatal(err)
	}
	if _, err = volume.ReplaceLyrics(ctx, "track", path, []string{lyricPath}, store.AdminAudit{}); err != nil {
		t.Fatal("remote lyric binding", err)
	}
	lyricCatalog, err := volume.LyricCatalog(ctx, "music/远端专辑", "lyrics", "", "")
	if err != nil || len(lyricCatalog.Tracks) != 1 || lyricCatalog.Tracks[0].MediaID != upload.MediaID || lyricCatalog.Tracks[0].LyricPath == nil || *lyricCatalog.Tracks[0].LyricPath != lyricPath {
		t.Fatal("global lyric catalog", lyricCatalog, err)
	}
	entries, err := volume.LyricsResource(ctx, path, upload.MediaID)
	if err != nil || len(entries) != 1 || entries[0].Text != "远端歌词" {
		t.Fatal("remote explicit lyric", entries, err)
	}
	auto, err := volume.AutoLyrics(ctx, store.AdminAudit{})
	if err != nil || auto.Preserved != 1 {
		t.Fatal("global auto lyrics replaced explicit link", auto, err)
	}
	var localCount int
	if err = masterDB.Database().QueryRow("SELECT COUNT(*) FROM media_objects WHERE media_id=?", upload.MediaID).Scan(&localCount); err != nil || localCount != 0 {
		t.Fatal("remote track manufactured local identity", localCount, err)
	}
	if err = masterDB.Nodes().SetRelationshipMode(ctx, relationID, "Relay", false, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	w = request(masterRouter, "GET", target, "")
	if w.Code != 503 {
		t.Fatal("Relay enabled without Nginx")
	}
	masterPublic.settings.NginxMedia = true
	w = request(masterRouter, "GET", target, "")
	if w.Code != 200 || !strings.HasPrefix(w.Header().Get("X-Accel-Redirect"), "/_relay_media/follower.test/443/"+upload.MediaID+"/") || w.Body.Len() != 0 {
		t.Fatal("Relay Nginx delegation", w.Code, w.Header(), w.Body.String())
	}
	if _, err = masterDB.Database().Exec("UPDATE node_relationships SET last_heartbeat=? WHERE relationship_id=?", time.Now().Unix()-120, relationID); err != nil {
		t.Fatal(err)
	}
	w = request(masterRouter, "GET", target, "")
	if w.Code != 503 || w.Header().Get("Retry-After") != "30" {
		t.Fatal("stale peer served", w.Code, w.Body.String())
	}
	// Revocation blocks already-issued capabilities, independently of expiry.
	if _, err = followerDB.Database().Exec("UPDATE node_relationships SET state='revoked' WHERE relationship_id=?", relationID); err != nil {
		t.Fatal(err)
	}
	w = request(followerRouter, "GET", location, "")
	if w.Code != 401 {
		t.Fatal("revoked capability accepted", w.Code)
	}
}

func TestNodeControlBoundsHTTPSMalformedJSONAndAuthentication(t *testing.T) {
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	router, _, _, _, _ := clusterFixture(t, "https://node.test", transport, true)
	for _, tc := range []struct {
		route, body string
		want        int
	}{{"http://node.test/internal/v1/confirm", "{}", 403}, {"https://node.test/internal/v1/pair", strings.Repeat("x", node.MaxControlBytes+1), 413}, {"https://node.test/internal/v1/pair", "{} {}", 409}, {"https://node.test/internal/v1/heartbeat", "{}", 401}, {"https://node.test/internal/v1/confirm", "{}", 401}, {"https://node.test/internal/v1/revoke", "{}", 401}} {
		w := request(router, "POST", tc.route, tc.body)
		if w.Code != tc.want {
			t.Fatalf("%s => %d %s", tc.route, w.Code, w.Body.String())
		}
	}
	r := httptest.NewRequest(http.MethodGet, "https://node.test/internal/v1/identity?challenge="+strings.Repeat("a", 32), nil)
	r.Header.Add("X-Forwarded-Proto", "https")
	r.Header.Add("X-Forwarded-Proto", "https")
	r.TLS = nil
	r.RemoteAddr = "127.0.0.1:1"
	w := httptest.NewRecorder()
	router.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal("untrusted duplicate forwarded protocol")
	}
}
