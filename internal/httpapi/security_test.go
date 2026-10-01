package httpapi

import (
	"context"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/security"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	storeSQLite "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func TestSecurityMiddlewarePersistsUnknownOperationsNotBusiness404s(t *testing.T) {
	root := t.TempDir()
	cfg, err := config.LoadFrom(func(key string) string {
		if key == "DATA_ROOT" {
			return root
		}
		return ""
	})
	if err != nil {
		t.Fatal(err)
	}
	db, err := storeSQLite.Open(filepath.Join(t.TempDir(), "db.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	s, err := security.New(cfg, db.Security())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	resolver, err := network.New([]string{"172.16.0.0/12"})
	if err != nil {
		t.Fatal(err)
	}
	ok := func(context.Context) error { return nil }
	router := NewWithResolver(ok, ok, resolver, SecurityMiddleware(s, resolver))
	router.GET("/business", func(c *gin.Context) { detail(c, 404, "missing object") })
	perform := func(method, target, peer, real string) int {
		r := httptest.NewRequest(method, "http://example.com"+target, nil)
		r.RemoteAddr = peer
		if real != "" {
			r.Header.Set("X-Real-IP", real)
		}
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		return w.Code
	}
	for range 8 {
		if code := perform("GET", "/business", "198.51.100.42:123", ""); code != 404 {
			t.Fatal("business error banned client", code)
		}
	}
	for range 6 {
		if code := perform("GET", "/unknown?token=not-a-real-secret", "198.51.100.42:123", "127.0.0.1"); code != 404 {
			t.Fatal("unknown operation response", code)
		}
	}
	if code := perform("GET", "/business", "198.51.100.42:123", ""); code != 403 {
		t.Fatal("durable ban not enforced", code)
	}
	if code := perform("GET", "/business", "172.20.0.2:123", ""); code != 400 {
		t.Fatal("trusted proxy missing identity accepted", code)
	}
	if code := perform("GET", "/business", "172.20.0.2:123", "198.51.100.42"); code != 403 {
		t.Fatal("verified proxy identity bypassed ban", code)
	}
	if _, err := db.Security().SetIPPolicy(context.Background(), "198.51.100.42", "unban", "", store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if code := perform("GET", "/business", "198.51.100.42:123", ""); code != 404 {
		t.Fatal("unban not immediate", code)
	}
	if code := perform("POST", "/business", "198.51.100.43:123", ""); code != 405 {
		t.Fatal("method not allowed contract", code)
	}
	for range 8 {
		if code := perform("GET", "/unknown", "127.0.0.1:123", ""); code != 404 {
			t.Fatal("exempt client banned", code)
		}
	}
	var count int
	if err := db.Database().QueryRow("SELECT COUNT(*) FROM ip_security_audit_log WHERE detail LIKE '%token%'").Scan(&count); err != nil || count != 0 {
		t.Fatal("query string was recorded in security audit", count, err)
	}
	db.Close()
	if code := perform("GET", "/business", "198.51.100.44:123", ""); code != 503 {
		t.Fatal("failed policy lookup bypassed security", code)
	}
}
