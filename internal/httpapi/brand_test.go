package httpapi

import (
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPublicBrandRedirectAndImmutableVersion(t *testing.T) {
	router, _, _, _ := publicFixture(t, true)
	w := request(router, "GET", "/api/v1/media/brand/logo/music", "")
	if w.Code != 307 || !strings.Contains(w.Header().Get("Location"), "?v=") {
		t.Fatalf("redirect: %d %v", w.Code, w.Header())
	}
	location := w.Header().Get("Location")
	w = request(router, "GET", location, "")
	if w.Code != 200 || !strings.Contains(w.Header().Get("Cache-Control"), "immutable") || !strings.HasPrefix(w.Header().Get("ETag"), `"brand-`) || w.Header().Get("Content-Type") != "image/webp" {
		t.Fatalf("asset: %d %v", w.Code, w.Header())
	}
	r := httptest.NewRequest("GET", location, nil)
	r.Header.Set("If-None-Match", w.Header().Get("ETag"))
	cached := httptest.NewRecorder()
	router.ServeHTTP(cached, r)
	if cached.Code != 304 {
		t.Fatalf("conditional request: %d", cached.Code)
	}
	if w := request(router, "GET", "/api/v1/media/brand/logo/unknown", ""); w.Code != 404 {
		t.Fatal("unknown kind accepted")
	}
}
