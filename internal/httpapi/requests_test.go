package httpapi

import (
	"net/http/httptest"
	"testing"
)

func TestRequestIDsOnlyTrustConfiguredProxy(t *testing.T) {
	router := New(pass, pass)
	for _, tc := range []struct {
		peer    string
		trusted bool
	}{{"172.20.0.1:123", true}, {"192.0.2.1:123", false}} {
		r := httptest.NewRequest("GET", "/health/live", nil)
		r.RemoteAddr = tc.peer
		r.Header.Set("X-Real-IP", "192.0.2.3")
		r.Header.Set("X-Request-ID", "caller-request")
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		if (w.Header().Get("X-Request-ID") == "caller-request") != tc.trusted {
			t.Fatalf("request ID trust: %v", w.Header())
		}
		if w.Header().Get("X-Audit-Trace-ID") == "" {
			t.Fatal("missing trace ID")
		}
	}
}
