package httpapi

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	sqlitestore "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func publicFixture(t *testing.T, accel bool) (*gin.Engine, *sqlitestore.Store, string, *Public) {
	t.Helper()
	dir := t.TempDir()
	for _, name := range []string{"music/artist", "music/nested/album", "vido/director", "lyrics"} {
		if err := os.MkdirAll(filepath.Join(dir, "media", name), 0755); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{"music/artist/song.mp3", "music/nested/album/音乐.mp3", "vido/director/video.mp4"} {
		if err := os.WriteFile(filepath.Join(dir, "media", name), []byte("0123456789"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	db, err := sqlitestore.Open(filepath.Join(dir, "data.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := db.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	identity, err := node.Initialize(context.Background(), db.Nodes(), filepath.Join(dir, "secrets"))
	if err != nil {
		t.Fatal(err)
	}
	svc, err := media.New(filepath.Join(dir, "media"), db.Media(), identity)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { svc.Close() })
	settings, err := config.LoadFrom(func(key string) string {
		if key == "STATIC_ROOT" {
			root, _ := filepath.Abs("../../static")
			return root
		}
		return ""
	})
	if err != nil {
		t.Fatal(err)
	}
	settings.NginxMedia = accel
	settings.DataRoot = dir
	router := New(pass, pass)
	public, err := RegisterPublic(router, settings, svc)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { public.Close() })
	return router, db, dir, public
}

func request(router *gin.Engine, method, target, body string) *httptest.ResponseRecorder {
	w := httptest.NewRecorder()
	r := httptest.NewRequest(method, target, strings.NewReader(body))
	if body != "" {
		r.Header.Set("Content-Type", "application/json")
	}
	router.ServeHTTP(w, r)
	return w
}

func TestPublicPagesAndCatalogContract(t *testing.T) {
	router, db, _, _ := publicFixture(t, true)
	for _, target := range []string{"/api/v1/media", "/api/v1/media/", "/api/v1/media/music", "/api/v1/media/video", "/api/v1/media/music/category?path=music/artist", "/api/v1/media/music/category?path=music/nested", "/api/v1/media/video/category?path=vido/director", "/api/v1/media/lyrics?track=music/artist/song.mp3", "/karaoke/"} {
		w := request(router, "GET", target, "")
		if w.Code != 200 || strings.Contains(w.Body.String(), "{{") {
			t.Fatalf("%s: %d unresolved=%v body=%.100s", target, w.Code, strings.Contains(w.Body.String(), "{{"), w.Body.String())
		}
	}
	w := request(router, "GET", "/api/v1/media/catalog/categories?media_type=music", "")
	if w.Code != 200 || !strings.Contains(w.Body.String(), "nested") || w.Header().Get("Cache-Control") != "private, no-cache, must-revalidate" {
		t.Fatalf("categories: %s", w.Body.String())
	}
	w = request(router, "GET", "/api/v1/media/catalog/media?media_type=music&path=music/artist&playback_session_id=test", "")
	var result struct{ Entries []media.Track }
	if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if len(result.Entries) != 1 || len(result.Entries[0].MediaID) != 64 || result.Entries[0].KaraokeID == "" || result.Entries[0].HasLyrics == nil || !*result.Entries[0].HasLyrics {
		t.Fatalf("catalog: %s", w.Body.String())
	}
	if _, err := db.Database().Exec("INSERT INTO media_visibility(relative_path,hidden,updated_at) VALUES ('music/artist',1,'2026-01-01')"); err != nil {
		t.Fatal(err)
	}
	w = request(router, "GET", "/api/v1/media/catalog/categories?media_type=music", "")
	if strings.Contains(w.Body.String(), `"name":"artist"`) {
		t.Fatal("hidden category exposed")
	}
	w = request(router, "GET", "/api/v1/media/catalog/categories?media_type=music&include_hidden=true", "")
	if !strings.Contains(w.Body.String(), `"name":"artist"`) {
		t.Fatal("reveal category omitted")
	}
	w = request(router, "GET", "/api/v1/media/lyrics/content?track=music/artist/song.mp3", "")
	if w.Code != 200 || !strings.Contains(w.Body.String(), "建设中，暂无歌词") {
		t.Fatalf("fallback lyric: %s", w.Body.String())
	}
}

func TestPublicStreamingAndPlaybackContract(t *testing.T) {
	router, _, _, _ := publicFixture(t, true)
	for _, method := range []string{"GET", "HEAD"} {
		w := request(router, method, "/api/v1/media/stream?file_path=music/artist/song.mp3", "")
		if w.Code != 200 || w.Header().Get("X-Accel-Redirect") != "/_protected_media/music/artist/song.mp3" || len(w.Header().Get("X-Media-Object-ID")) != 64 || len(w.Header().Get("X-Media-Resource-ID")) != 64 {
			t.Fatalf("stream: %d %v", w.Code, w.Header())
		}
	}
	if w := request(router, "GET", "/api/v1/media/stream?file_path=music/artist/missing.mp3", ""); w.Code != 404 {
		t.Fatalf("missing status %d", w.Code)
	}
	body := `{"media_path":"music/artist/song.mp3","playback_session_id":"2775bcdc-9f7d-42b5-a3c3-c10caa131bee","played_seconds":30,"duration":60}`
	for i := 0; i < 2; i++ {
		w := request(router, "POST", "/api/v1/media/playback", body)
		var result map[string]any
		json.Unmarshal(w.Body.Bytes(), &result)
		if w.Code != 200 || result["counted"] != (i == 0) || result["play_score"] != float64(1) || result["threshold_seconds"] != float64(30) {
			t.Fatalf("accounting: %s", w.Body.String())
		}
	}
	w := request(router, "POST", "/api/v1/media/playback", strings.Replace(body, `"played_seconds":30`, `"played_seconds":3`, 1))
	if w.Code != 400 {
		t.Fatalf("threshold: %s", w.Body.String())
	}
}

func TestPublicRangeAndPathSecurity(t *testing.T) {
	router, _, dir, _ := publicFixture(t, false)
	r := httptest.NewRequest("GET", "/api/v1/media/stream?file_path=music/artist/song.mp3", nil)
	r.Header.Set("Range", "bytes=2-5")
	w := httptest.NewRecorder()
	router.ServeHTTP(w, r)
	if w.Code != 206 || w.Body.String() != "2345" {
		t.Fatalf("range: %d %q", w.Code, w.Body.String())
	}
	for _, target := range []string{"/api/v1/media/stream?file_path=../data.db", "/api/v1/media/stream?file_path=music/artist/../../data.db", "/api/v1/media/stream?file_path=lyrics/default.lrc"} {
		if w := request(router, "GET", target, ""); w.Code != 403 {
			t.Fatalf("unsafe path %s: %d", target, w.Code)
		}
	}
	for _, target := range []string{"/api/v1/media/catalog/categories?media_type=audio", "/api/v1/media/catalog/categories?media_type=music&include_hidden=maybe", "/api/v1/media/catalog/media?media_type=music&path=music/artist"} {
		if w := request(router, "GET", target, ""); w.Code != 422 {
			t.Fatalf("validation %s: %d", target, w.Code)
		}
	}
	if err := os.Symlink(filepath.Join(dir, "media", "music", "artist"), filepath.Join(dir, "media", "music", "symlink")); err == nil {
		if w := request(router, "GET", "/api/v1/media/stream?file_path=music/symlink/song.mp3", ""); w.Code != 403 {
			t.Fatalf("symlink accepted: %d", w.Code)
		}
	}
}
