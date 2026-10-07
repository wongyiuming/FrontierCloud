package business_test

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestKaraokeRegistrationQuotaConcurrencyPasswordAndDurableDeletion(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	d := db.(interface{ Database() *sql.DB }).Database()
	n, err := db.Nodes().InitializeIdentity(ctx, store.NodeIdentity{ID: strings.Repeat("a", 32), Role: "Standalone", PrivateKey: "encrypted-fixture", CreatedAt: time.Now().Unix()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Nodes().PromoteIdentity(ctx, store.NodePromotion{Role: "Master", Endpoint: "https://master.test", Allocation: 5 * store.GiB, PhysicalFree: 10 * store.GiB}, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	repo := db.Karaoke()
	ip, day := "192.0.2.171", "20261001"
	t.Cleanup(func() {
		d.Exec("DELETE FROM karaoke_audit_log WHERE client_ip=?", ip)
		d.Exec("DELETE FROM karaoke_users WHERE username_key LIKE ?", "native-account-%")
		d.Exec("DELETE FROM karaoke_registration_daily WHERE client_ip=?", ip)
		for _, table := range []string{"cluster_storage_members", "cluster_compute_members", "cluster_backup_members"} {
			d.Exec("DELETE FROM "+table+" WHERE member_id=?", n.ID)
		}
		d.Exec("UPDATE node_identity SET `role`=?,endpoint=? WHERE singleton=1", n.Role, n.Endpoint)
	})
	var wg sync.WaitGroup
	var success atomic.Int32
	users := make(chan store.KaraokeUser, 16)
	for i := range 16 {
		wg.Go(func() {
			v := store.KaraokeUser{ID: fmt.Sprintf("%032x", i+100), Username: fmt.Sprintf("native-account-%d", i), NameKey: fmt.Sprintf("native-account-%d", i), PasswordHash: "fixture-scrypt", Quota: 200 * 1024 * 1024}
			err := repo.RegisterUser(ctx, v, ip, day, store.KaraokeAudit{IP: ip})
			if err == nil {
				success.Add(1)
				users <- v
			} else if !errors.Is(err, store.ErrRegistrationLimit) {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	close(users)
	if success.Load() != 3 {
		t.Fatal("daily limit raced", success.Load())
	}
	counts, err := repo.RegistrationCounts(ctx, ip, day)
	if err != nil || counts.Successes != 3 {
		t.Fatal(counts, err)
	}
	for i := range 30 {
		wg.Go(func() {
			if err := repo.RegistrationFailure(ctx, ip, day, store.KaraokeAudit{IP: ip, RequestID: fmt.Sprint(i)}); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	counts, err = repo.RegistrationCounts(ctx, ip, day)
	if err != nil || counts.Failures != 20 {
		t.Fatal("failure limit raced", counts, err)
	}
	u := <-users
	if err := repo.ChangePassword(ctx, u.ID, "wrong-old-hash", "new-hash", store.KaraokeAudit{IP: ip}); !errors.Is(err, store.ErrAccountConflict) {
		t.Fatal("password compare-and-swap bypass", err)
	}
	if err := repo.ChangePassword(ctx, u.ID, u.PasswordHash, "new-hash", store.KaraokeAudit{IP: ip}); err != nil {
		t.Fatal(err)
	}
	if err := repo.ConfirmLogin(ctx, u.ID, u.PasswordHash, store.KaraokeAudit{IP: ip}); !errors.Is(err, store.ErrAccountConflict) {
		t.Fatal("stale login after password change", err)
	}
	if err := repo.MutateUser(ctx, u.ID, "ban", 0, store.KaraokeAudit{IP: ip}); err != nil {
		t.Fatal(err)
	}
	if err := repo.ConfirmLogin(ctx, u.ID, "new-hash", store.KaraokeAudit{IP: ip}); !errors.Is(err, store.ErrUserBlocked) {
		t.Fatal("banned login", err)
	}
	if err := repo.MutateUser(ctx, u.ID, "unban", 0, store.KaraokeAudit{IP: ip}); err != nil {
		t.Fatal(err)
	}
	if _, err := d.Exec("UPDATE karaoke_users SET used_bytes=? WHERE user_id=?", 2*1024*1024, u.ID); err != nil {
		t.Fatal(err)
	}
	if err := repo.MutateUser(ctx, u.ID, "quota", 1024*1024, store.KaraokeAudit{IP: ip}); !errors.Is(err, store.ErrUserQuota) {
		t.Fatal("quota below usage", err)
	}
	if found, err := repo.StageUserDeletion(ctx, u.ID, store.KaraokeAudit{Action: "account-delete", IP: ip}); err != nil || !found {
		t.Fatal(found, err)
	}
	if complete, err := repo.FinishUserDeletion(ctx, u.ID, store.KaraokeAudit{Action: "account-delete", IP: ip}); !errors.Is(err, store.ErrAccountConflict) || complete {
		t.Fatal("unaccounted capacity erased", complete, err)
	}
	if err := repo.MutateUser(ctx, u.ID, "unban", 0, store.KaraokeAudit{IP: ip}); !errors.Is(err, store.ErrAccountConflict) {
		t.Fatal("deleting user resurrected", err)
	}
	if _, err := d.Exec("UPDATE karaoke_users SET used_bytes=0 WHERE user_id=?", u.ID); err != nil {
		t.Fatal(err)
	}
	for range 16 {
		wg.Go(func() {
			if complete, err := repo.FinishUserDeletion(ctx, u.ID, store.KaraokeAudit{Action: "account-delete", IP: ip}); err != nil || !complete {
				t.Error(complete, err)
			}
		})
	}
	wg.Wait()
	user, err := repo.UserByID(ctx, u.ID)
	if err != nil || user != nil {
		t.Fatal("deleted user remains", user, err)
	}
	var count int
	if err := d.QueryRow("SELECT COUNT(*) FROM karaoke_audit_log WHERE user_id=? AND action='account-delete' AND result='success'", u.ID).Scan(&count); err != nil || count != 1 {
		t.Fatal("deletion duplicated audit", count, err)
	}
}
