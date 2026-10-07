package httpapi

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/diagnostics"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestPlaybackDiagnosticsStandaloneBoundsRetirementAndNonPersistence(t *testing.T) {
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	router, _, _, p, service := clusterFixture(t, "https://diagnostic.test", transport, true)
	resolver, _ := network.New(nil)
	reports := diagnostics.New()
	now := int64(1790800000)
	registerPlaybackDiagnostics(router, p.settings, resolver, service, &Admin{settings: p.settings, network: resolver}, reports, func() int64 { return now })
	path := "https://diagnostic.test/api/v1/media/playback-continuity-diagnostics"
	for _, tc := range []struct {
		body string
		code int
	}{
		{`{"diagnostic_id":"case","stage":"pause","sample":{"currentSrc":"private","paused":true}}`, 202},
		{`{}`, 400}, {`[]`, 400}, {`null`, 400}, {`{"diagnostic_id":"case"}`, 400}, {`{} {}`, 400},
		{strings.Repeat("x", diagnostics.MaxBodyBytes+1), 413},
	} {
		w := request(router, "POST", path, tc.body)
		if w.Code != tc.code || w.Header().Get("Cache-Control") != "no-store" {
			t.Fatal(w.Code, w.Body.String(), w.Header())
		}
		if tc.code == 202 && w.Body.Len() != 0 {
			t.Fatal("accepted response has body")
		}
	}
	values := reports.Snapshot(now)["reports"].([]map[string]any)
	if len(values) != 1 || values[0]["delivery"] != "standalone-local" || values[0]["sample"].(map[string]any)["paused"] != true {
		t.Fatal(values)
	}
	if _, ok := values[0]["sample"].(map[string]any)["currentSrc"]; ok {
		t.Fatal("private media URL retained")
	}
	// An advertised-small streaming body must still be bounded by actual bytes.
	r := httptest.NewRequest("POST", path, strings.NewReader(strings.Repeat("x", diagnostics.MaxBodyBytes+1)))
	r.ContentLength = -1
	w := httptest.NewRecorder()
	router.ServeHTTP(w, r)
	if w.Code != 413 {
		t.Fatal(w.Code)
	}
	now = 1792022400 // 2026-10-15T00:00:00Z: retirement precedes parsing/authentication.
	if w = request(router, "POST", path, "invalid"); w.Code != 410 {
		t.Fatal(w.Code, w.Body.String())
	}
	if w = request(router, "POST", "https://diagnostic.test"+diagnosticRelayPath, "invalid"); w.Code != 410 {
		t.Fatal(w.Code, w.Body.String())
	}
}

func TestPlaybackDiagnosticsSignedRelayFallbackDirectionAndRevocation(t *testing.T) {
	ctx := context.Background()
	transport := &clusterHTTP{routers: map[string]*gin.Engine{}}
	type site struct {
		router  *gin.Engine
		service *node.Service
		reports *diagnostics.Store
		origin  string
	}
	create := func(origin, role string) site {
		router, _, _, p, service := clusterFixture(t, origin, transport, true)
		resolver, _ := network.New(nil)
		reports := diagnostics.New()
		registerPlaybackDiagnostics(router, p.settings, resolver, service, &Admin{settings: p.settings, network: resolver}, reports, func() int64 { return 1790800000 })
		allocation := int64(0)
		if role == "Master" {
			allocation = store.GiB
		}
		if _, err := service.Promote(ctx, role, origin, allocation, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
		return site{router, service, reports, origin}
	}
	m := create("https://diagnostic-master.test", "Master")
	f := create("https://diagnostic-follower.test", "Follower")
	pack, err := f.service.CreatePair(ctx, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	id, err := m.service.ImportPair(ctx, pack, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	payload := `{"diagnostic_id":"relay","stage":"pause","sample":{"token":"secret","safe":true}}`
	path := f.origin + "/api/v1/media/playback-continuity-diagnostics"
	if w := request(f.router, "POST", path, payload); w.Code != 202 {
		t.Fatal(w.Code, w.Body.String())
	}
	masterReports := m.reports.Snapshot(1790800000)["reports"].([]map[string]any)
	identity, _ := f.service.IdentityState(ctx)
	if len(masterReports) != 1 || masterReports[0]["source_node"] != identity.ID || masterReports[0]["delivery"] != "follower-relay" || len(f.reports.Snapshot(1790800000)["reports"].([]map[string]any)) != 0 {
		t.Fatal(masterReports)
	}
	upstream, err := f.service.DiagnosticUpstream(ctx)
	if err != nil || upstream == nil {
		t.Fatal(err)
	}
	// The Master's downstream credential cannot authorize a relay into a Follower.
	masterStatus, err := m.service.Status(ctx)
	if err != nil {
		t.Fatal(err)
	}
	relations := masterStatus["relationships"].([]store.Relationship)
	if _, err = m.service.Call(ctx, relations[0], diagnosticRelayPath, map[string]any{"diagnostic_id": "x", "stage": "pause"}); err == nil {
		t.Fatal("reverse relay accepted")
	}
	if w := request(m.router, "POST", m.origin+diagnosticRelayPath, payload); w.Code != 401 {
		t.Fatal(w.Code)
	}
	if w := request(m.router, "POST", "http://diagnostic-master.test"+diagnosticRelayPath, payload); w.Code != 403 {
		t.Fatal(w.Code)
	}
	if _, err = transport.Request(ctx, m.origin, diagnosticRelayPath, "POST", map[string]any{"diagnostic_id": "tamper", "stage": "pause"}, id, strings.Repeat("a", 64)); err == nil {
		t.Fatal("invalid signature accepted")
	}
	delete(transport.routers, m.origin)
	if w := request(f.router, "POST", path, payload); w.Code != 202 {
		t.Fatal(w.Code, w.Body.String())
	}
	fallback := f.reports.Snapshot(1790800000)["reports"].([]map[string]any)
	if len(fallback) != 1 || fallback[0]["delivery"] != "follower-fallback" {
		t.Fatal(fallback)
	}
	transport.routers[m.origin] = m.router
	if err = m.service.Revoke(ctx, id, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, err = f.service.Call(ctx, *upstream, diagnosticRelayPath, map[string]any{"diagnostic_id": "revoked", "stage": "pause"}); err == nil {
		t.Fatal("revoked relay accepted")
	}
	if len(m.reports.Snapshot(1790800000)["reports"].([]map[string]any)) != 1 {
		t.Fatal("rejected relay changed store")
	}
}
