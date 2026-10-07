package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/karaoke"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/recording"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestRecordingAccountHTTPRealRedisPrivateCSRFSizeFinalizeAndUserDeletion(t *testing.T) {
	raw := os.Getenv("FRONTIERCLOUD_TEST_REDIS_URL")
	if raw == "" {
		t.Skip("disposable Redis not configured")
	}
	opts, e := redis.ParseURL(raw)
	if e != nil {
		t.Fatal(e)
	}
	cache := redis.NewClient(opts)
	defer cache.Close()
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	master := nativeRecordingFixture(t, "https://record-master.test", "Master", transport)
	resolver, _ := network.New(nil)
	accountsService := karaoke.New(master.db.Karaoke(), master.db.Nodes(), karaoke.NewRedisCache(cache))
	accounts := RegisterKaraokeAccounts(master.router, accountsService, master.public, nil, resolver)
	manager := recording.NewManager(master.db.Recordings(), master.db.Karaoke(), master.db.Nodes(), master.db.Pool(), master.control, master.volume)
	RegisterKaraokeRecordings(master.router, accounts, manager, master.volume)
	u := store.KaraokeUser{ID: strings.Repeat("c", 32), Username: "record-http", NameKey: "record-http", PasswordHash: "fixture", Quota: 1024 * 1024}
	if e = master.db.Karaoke().RegisterUser(ctx, u, "192.0.2.181", "20261001", store.KaraokeAudit{}); e != nil {
		t.Fatal(e)
	}
	token, csrf := strings.Repeat("x", 43), strings.Repeat("c", 32)
	key := "karaoke:session:" + karaokeSessionHash(token)
	if e = karaoke.NewRedisCache(cache).SaveSession(ctx, key, u.ID, csrf, karaokeSessionHash(u.PasswordHash)); e != nil {
		t.Fatal(e)
	}
	defer cache.Del(ctx, key, "karaoke:user-generation:"+u.ID)
	perform := func(method, target, body string, auth, withCSRF bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "https://record-master.test"+target, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
		if auth {
			r.AddCookie(&http.Cookie{Name: "__Host-karaoke_session", Value: token})
			r.AddCookie(&http.Cookie{Name: "__Host-karaoke_csrf", Value: csrf})
		}
		if withCSRF {
			r.Header.Set("X-Karaoke-CSRF", csrf)
		}
		if strings.Contains(target, "/stream") {
			r.Header.Set("Range", "bytes=1-3")
		}
		w := httptest.NewRecorder()
		master.router.ServeHTTP(w, r)
		return w
	}
	const base = "/api/v1/karaoke/account/recordings"
	if w := perform("GET", base, "", false, false); w.Code != 401 {
		t.Fatal("anonymous private recordings", w.Code, w.Body.String())
	}
	if w := perform("POST", base+"/ticket", `{"size_bytes":8,"content_type":"audio/webm"}`, true, false); w.Code != 403 {
		t.Fatal("missing CSRF", w.Code, w.Body.String())
	}
	ticket := func() recording.Ticket {
		w := perform("POST", base+"/ticket", `{"size_bytes":8,"content_type":"audio/webm","title":"标题/安全"}`, true, true)
		var value recording.Ticket
		if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &value) != nil || strings.Contains(value.Filename, "/") || value.Direct {
			t.Fatal("ticket", w.Code, w.Body.String())
		}
		return value
	}
	v := ticket()
	if w := perform("PUT", v.URL, "short", true, true); w.Code != 400 {
		t.Fatal("short upload", w.Code, w.Body.String())
	}
	if w := perform("PUT", v.URL, "12345678", true, true); w.Code != 200 {
		t.Fatal("upload", w.Code, w.Body.String())
	}
	for range 2 {
		if w := perform("POST", base+"/"+v.ID+"/finalize", `{"sha256":"forged-client","size_bytes":1}`, true, true); w.Code != 200 {
			t.Fatal("server stat finalize", w.Code, w.Body.String())
		}
	}
	w := perform("GET", base, "", true, false)
	if w.Code != 200 || !strings.Contains(w.Body.String(), v.ID) || strings.Contains(w.Body.String(), u.ID) || strings.Contains(w.Body.String(), "storage_member_id") {
		t.Fatal("private listing", w.Code, w.Body.String())
	}
	for _, method := range []string{"GET", "HEAD"} {
		w = perform(method, base+"/"+v.ID+"/stream", "", true, false)
		if w.Code != 206 || !strings.Contains(w.Header().Get("Cache-Control"), "no-store") || method == "GET" && w.Body.String() != "234" || method == "HEAD" && w.Body.Len() != 0 {
			t.Fatal(method, w.Code, w.Body.String(), w.Header())
		}
	}
	w = perform("GET", base+"/"+v.ID+"/download", "", true, false)
	if w.Code != 200 || !strings.HasPrefix(w.Header().Get("Content-Disposition"), "attachment;") {
		t.Fatal("download", w.Code, w.Header())
	}
	if w = perform("DELETE", base+"/"+v.ID, "", true, false); w.Code != 403 {
		t.Fatal("unauthorized deletion", w.Code)
	}
	for range 2 {
		if w = perform("DELETE", base+"/"+v.ID, "", true, true); w.Code != 200 {
			t.Fatal("delete/replay", w.Code, w.Body.String())
		}
	}
	ready, pending := ticket(), ticket()
	if w = perform("PUT", ready.URL, "12345678", true, true); w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	if w = perform("POST", base+"/"+ready.ID+"/finalize", "", true, true); w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	if w = perform("DELETE", "/api/v1/karaoke/account", "", true, true); w.Code != 200 || !strings.Contains(w.Body.String(), `"status":"deleted"`) {
		t.Fatal("account physical cleanup", w.Code, w.Body.String())
	}
	for _, id := range []string{ready.ID, pending.ID} {
		row, e := master.db.Recordings().Recording(ctx, id)
		if e != nil || row != nil {
			t.Fatal(row, e)
		}
	}
	row, e := master.db.Karaoke().UserByID(ctx, u.ID)
	if e != nil || row != nil {
		t.Fatal("deleting user not finished", row, e)
	}
	members, e := master.db.Pool().Members(ctx)
	if e != nil || members[0].Reserved != 0 || members[0].Used != 0 {
		t.Fatal("account capacity cleanup", members, e)
	}
}

func TestKaraokeOpaqueContextLyricsStreamAndInvalidHandles(t *testing.T) {
	router, _, _, public := publicFixture(t, false)
	resolver, _ := network.New(nil)
	RegisterKaraokeMedia(router, public, resolver)
	w := request(router, "GET", "/api/v1/media/catalog/media?media_type=music&path=music/artist&playback_session_id=karaoke-test", "")
	var catalog struct{ Entries []media.Track }
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &catalog) != nil || len(catalog.Entries) != 1 {
		t.Fatal(w.Code, w.Body.String())
	}
	handle := catalog.Entries[0].KaraokeID
	query := url.Values{"media": {handle}}.Encode()
	w = request(router, "GET", "/api/v1/karaoke/context?"+query, "")
	if w.Code != 200 || strings.Contains(w.Body.String(), catalog.Entries[0].MediaID) || !strings.Contains(w.Body.String(), `"title":"song"`) {
		t.Fatal("opaque context", w.Code, w.Body.String())
	}
	for _, route := range []string{"stream", "lyrics"} {
		w = request(router, "GET", "/api/v1/karaoke/"+route+"?"+query, "")
		if w.Code != 200 || !strings.Contains(w.Header().Get("Cache-Control"), "no-store") {
			t.Fatal(route, w.Code, w.Body.String(), w.Header())
		}
	}
	for _, suffix := range []string{url.QueryEscape("bad"), url.QueryEscape(handle + "bad")} {
		w = request(router, "GET", "/api/v1/karaoke/context?media="+suffix, "")
		if w.Code != 404 {
			t.Fatal("invalid handle", w.Code)
		}
	}
	w = request(router, "GET", "/api/v1/karaoke/context?"+query+"&"+query, "")
	if w.Code != 422 {
		t.Fatal("duplicate handle", w.Code)
	}
}
