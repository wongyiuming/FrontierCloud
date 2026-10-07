package httpapi

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	storeSQLite "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func TestSignedNodeIdentityRequiresVerifiedHTTPS(t *testing.T) {
	dir := t.TempDir()
	db, err := storeSQLite.Open(filepath.Join(dir, "db.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err = db.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	identity, err := node.Initialize(context.Background(), db.Nodes(), filepath.Join(dir, "secrets"))
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := network.New([]string{"172.20.0.0/16"})
	if err != nil {
		t.Fatal(err)
	}
	cfg := config.Config{TLSEnabled: true}
	service := node.NewService(db.Nodes(), identity, nil)
	router := NewWithResolver(pass, pass, resolver)
	RegisterNodeIdentity(router, cfg, resolver, service)
	for _, tc := range []struct {
		url, peer, forwarded string
		want                 int
	}{
		{"http://host/internal/v1/identity?challenge=" + strings.Repeat("a", 32), "127.0.0.1:1", "", 403},
		{"http://host/internal/v1/identity?challenge=" + strings.Repeat("a", 32), "192.0.2.1:1", "https", 403},
		{"http://host/internal/v1/identity?challenge=" + strings.Repeat("a", 32), "172.20.0.1:1", "https", 200},
		{"https://host/internal/v1/identity?challenge=" + strings.Repeat("a", 32), "192.0.2.1:1", "", 200},
		{"https://host/internal/v1/identity?challenge=" + strings.Repeat("z", 32), "192.0.2.1:1", "", 400},
		{"https://host/internal/v1/identity?challenge=" + strings.Repeat("a", 32) + "&challenge=" + strings.Repeat("b", 32), "192.0.2.1:1", "", 400},
	} {
		r := httptest.NewRequest("GET", tc.url, nil)
		r.RemoteAddr = tc.peer
		if tc.forwarded != "" {
			r.Header.Set("X-Real-IP", "192.0.2.2")
			r.Header.Set("X-Forwarded-Proto", tc.forwarded)
		}
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		if w.Code != tc.want {
			t.Fatalf("%+v => %d %s", tc, w.Code, w.Body.String())
		}
		if w.Header().Get("Cache-Control") != "no-store" {
			t.Fatal("identity caching enabled")
		}
		if tc.want == 200 {
			var signed node.Envelope
			decoder := json.NewDecoder(w.Body)
			decoder.UseNumber()
			if err = decoder.Decode(&signed); err != nil {
				t.Fatal(err)
			}
			public, _ := signed.Payload["public_key"].(string)
			if err = protocol.Verify(public, signed.Payload, signed.Signature); err != nil || signed.Payload["node_id"] != identity.ID || signed.Payload["role"] != "Standalone" {
				t.Fatal("signed identity contract", err)
			}
		}
	}
}
