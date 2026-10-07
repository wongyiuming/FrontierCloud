package security

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	storeSQLite "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func TestPolicyPublishesNginxSnapshotAndPreservesDurableEnforcement(t *testing.T) {
	root := t.TempDir()
	settings, err := config.LoadFrom(func(key string) string {
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
	ctx := context.Background()
	if err := db.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	s, err := New(settings, db.Security())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if err := s.Publish(ctx, true); err != nil {
		t.Fatal(err)
	}
	if err := s.Ready(ctx); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Policy(ctx, "127.0.0.1", "reban", "invalid exempt", store.AdminAudit{}); !errors.Is(err, store.ErrPolicy) {
		t.Fatal("exempt IP banned")
	}
	if _, _, err := s.Policy(ctx, "198.51.100.11", "permanent_ban", "reason", store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Policy(ctx, "198.51.100.2", "reban", "reason", store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	content, err := os.ReadFile(filepath.Join(root, ".ip-security/active-bans.tsv"))
	if err != nil || !strings.Contains(string(content), "198.51.100.11\t0\n") || strings.Index(string(content), "198.51.100.2\t") > strings.Index(string(content), "198.51.100.11\t") {
		t.Fatalf("edge numeric order: %s %v", content, err)
	}
	if err := os.Remove(filepath.Join(root, ".ip-security/active-bans.tsv")); err != nil {
		t.Fatal(err)
	}
	if err := s.Ready(ctx); err == nil {
		t.Fatal("missing snapshot passed readiness")
	}
	if err := s.Publish(ctx, false); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Policy(ctx, "198.51.100.11", "whitelist", "allow", store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	ban, err := s.Block(ctx, "198.51.100.11")
	if err != nil || ban != nil {
		t.Fatal("whitelist did not override")
	}
	content, err = os.ReadFile(filepath.Join(root, ".ip-security/active-bans.tsv"))
	if err != nil || strings.Contains(string(content), "198.51.100.11") {
		t.Fatal("whitelist retained in edge ban")
	}
	if ban, err := s.Block(ctx, "198.51.100.2"); err != nil || ban == nil {
		t.Fatal("durable block unavailable without Redis")
	}
	for _, filter := range []string{"ab", "192%", "bad.example", "192.0.2.1 OR 1=1"} {
		if _, err := FilterIP(filter, "fuzzy"); err == nil {
			t.Fatalf("invalid IP fragment accepted %s", filter)
		}
	}
}
