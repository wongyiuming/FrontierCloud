package business_test

import (
	"context"
	"database/sql"
	"errors"
	"sync"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestDurableIPSecurityConcurrentAccountingPoliciesAndSummary(t *testing.T) {
	db := database(t)
	repo := db.Security()
	ctx := context.Background()
	ip := "198.51.100.42"
	var workers sync.WaitGroup
	failures := make(chan error, 16)
	for range 16 {
		workers.Go(func() {
			_, err := repo.RecordInvalidAPI(ctx, ip, "GET", "/unknown", "agent", 5, 3600, store.AdminAudit{RequestID: "security-test"})
			if err != nil {
				failures <- err
			}
		})
	}
	workers.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	ban, err := repo.IPBlock(ctx, ip)
	if err != nil || ban == nil || ban.Kind != "auto" {
		t.Fatalf("first offense: %+v %v", ban, err)
	}
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	var attackCount, eventCount int
	if err := sqlDB.QueryRow("SELECT attack_count FROM ip_security_summary WHERE ip_address=?", ip).Scan(&attackCount); err != nil || attackCount != 16 {
		t.Fatalf("lost attacks %d %v", attackCount, err)
	}
	if err := sqlDB.QueryRow("SELECT COUNT(*) FROM ip_auto_ban_events WHERE ip_address=?", ip).Scan(&eventCount); err != nil || eventCount != 1 {
		t.Fatalf("duplicate auto ban %d %v", eventCount, err)
	}
	if _, err := repo.SetIPPolicy(ctx, ip, "unban", "", store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if ban, err := repo.IPBlock(ctx, ip); err != nil || ban != nil {
		t.Fatal("unban did not release")
	}
	if _, err := repo.RecordInvalidAPI(ctx, ip, "GET", "/again", "agent", 5, 3600, store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	ban, err = repo.IPBlock(ctx, ip)
	if err != nil || ban == nil || ban.Kind != "permanent" || ban.ExpiresAt.Year() != 9999 {
		t.Fatalf("second offense: %+v %v", ban, err)
	}
	if _, err := repo.SetIPPolicy(ctx, ip, "whitelist", "allow", store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	if count, err := repo.RecordInvalidAPI(ctx, ip, "GET", "/exempt", "agent", 5, 3600, store.AdminAudit{}); err != nil || count != 0 {
		t.Fatalf("whitelist accounted attack: %d %v", count, err)
	}
	if _, err := repo.SetIPPolicy(ctx, ip, "reban", "manual", store.AdminAudit{}); !errors.Is(err, store.ErrPolicy) {
		t.Fatal("whitelist can be banned", err)
	}
	if _, err := repo.SetIPPolicy(ctx, ip, "whitelist_remove", "", store.AdminAudit{}); err != nil {
		t.Fatal(err)
	}
	manual, err := repo.SetIPPolicy(ctx, ip, "reban", "manual", store.AdminAudit{})
	if err != nil || manual == nil || manual.Kind != "manual" {
		t.Fatalf("manual: %+v %v", manual, err)
	}
	permanent, err := repo.SetIPPolicy(ctx, ip, "permanent_ban", "permanent", store.AdminAudit{})
	if err != nil || permanent == nil || permanent.Kind != "permanent" {
		t.Fatalf("permanent: %+v %v", permanent, err)
	}
	if _, err := repo.SetIPPolicy(ctx, ip, "permanent_ban", "duplicate", store.AdminAudit{}); !errors.Is(err, store.ErrPolicy) {
		t.Fatal("duplicate permanent allowed", err)
	}
	for _, address := range []string{"10.11.0.1", "10.2.0.1", "2001:db8::1"} {
		if _, err := repo.RecordInvalidAPI(ctx, address, "GET", "/missing", "", 5, 3600, store.AdminAudit{}); err != nil {
			t.Fatal(err)
		}
	}
	f := store.SecurityFilter{MatchMode: "exact", IPOrder: "asc", Page: 1, PageSize: 100, Window: 3600}
	summary, err := repo.SecuritySummary(ctx, f)
	if err != nil || summary.Total != 4 || summary.Events[0].IP != "10.2.0.1" || summary.Events[1].IP != "10.11.0.1" || summary.ActiveCount != 1 {
		t.Fatalf("numeric summary order: %+v %v", summary, err)
	}
	f.IP = ip
	summary, err = repo.SecuritySummary(ctx, f)
	if err != nil || len(summary.Events) != 1 || !summary.Events[0].Permanent || summary.Events[0].AttackCount != 17 || summary.Events[0].BanCount != 4 {
		t.Fatalf("single policy object per IP: %+v %v", summary, err)
	}
	f.IP = "10."
	f.MatchMode = "fuzzy"
	f.PageSize = 50
	summary, err = repo.SecuritySummary(ctx, f)
	if err != nil || summary.Total != 2 {
		t.Fatalf("fuzzy summary: %+v %v", summary, err)
	}
	bans, generation, err := repo.EdgeBans(ctx)
	if err != nil || len(bans) != 1 || !bans[0].Permanent {
		t.Fatalf("edge policy: %+v %v", bans, err)
	}
	if err := repo.AcknowledgeEdge(ctx, generation); err != nil {
		t.Fatal(err)
	}
	current, published, err := repo.EdgeProjection(ctx)
	if err != nil || current != published {
		t.Fatalf("edge generation %d %d %v", current, published, err)
	}
}

func TestSecurityAuditFailureRollsBackPolicy(t *testing.T) {
	db := database(t)
	if db.Backend() != "sqlite" {
		t.Skip("SQLite audit trigger failure injection")
	}
	repo := db.Security()
	ctx := context.Background()
	sqlDB := db.(interface{ Database() *sql.DB }).Database()
	if _, err := sqlDB.Exec("CREATE TRIGGER security_audit_fault BEFORE INSERT ON admin_audit_log BEGIN SELECT RAISE(FAIL,'audit unavailable'); END"); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.SetIPPolicy(ctx, "198.51.100.99", "reban", "manual", store.AdminAudit{}); err == nil {
		t.Fatal("unaudited policy committed")
	}
	ban, err := repo.IPBlock(ctx, "198.51.100.99")
	if err != nil || ban != nil {
		t.Fatalf("policy survived rollback: %+v %v", ban, err)
	}
	var count int
	if err := sqlDB.QueryRow("SELECT COUNT(*) FROM ip_security_audit_log").Scan(&count); err != nil || count != 0 {
		t.Fatalf("partial audit %d %v", count, err)
	}
}
