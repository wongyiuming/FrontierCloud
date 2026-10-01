package httpapi

import (
	"net/http/httptest"
	"regexp"
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

func TestTraceparentCannotSpoofAuditIdentity(t *testing.T) {
	router := New(pass, pass)
	trace := "1234567890abcdef1234567890abcdef"
	for _, tc := range []struct {
		peer, parent string
		accepted     bool
	}{
		{"172.20.0.1:123", "00-" + trace + "-1234567890abcdef-01", true},
		{"192.0.2.1:123", "00-" + trace + "-1234567890abcdef-01", false},
		{"172.20.0.1:123", "00-00000000000000000000000000000000-1234567890abcdef-01", false},
		{"172.20.0.1:123", "00-" + trace + "-0000000000000000-01", false},
		{"172.20.0.1:123", "invalid", false},
	} {
		r := httptest.NewRequest("GET", "/health/live", nil)
		r.RemoteAddr = tc.peer
		r.Header.Set("X-Real-IP", "192.0.2.3")
		r.Header.Set("X-Request-ID", "non-hex-request-id")
		r.Header.Set("Traceparent", tc.parent)
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		got := w.Header().Get("X-Audit-Trace-ID")
		if (got == trace) != tc.accepted || !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(got) {
			t.Fatalf("trace trust: %+v => %s", tc, got)
		}
	}
}
