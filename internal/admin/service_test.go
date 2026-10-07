package admin

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type memoryCache struct {
	mu        sync.Mutex
	fail      bool
	attempts  map[string]int64
	sessions  map[string]map[string]string
	temporary map[string]string
	refreshes int
	lock      string
}

func newCache() *memoryCache {
	return &memoryCache{attempts: map[string]int64{}, sessions: map[string]map[string]string{}, temporary: map[string]string{}}
}
func (c *memoryCache) ReserveAttempt(_ context.Context, key string, _ int) (int64, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.fail {
		return 0, errors.New("offline")
	}
	c.attempts[key]++
	return c.attempts[key], nil
}
func (c *memoryCache) Delete(_ context.Context, key string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.sessions, key)
	delete(c.attempts, key)
	delete(c.temporary, key)
	return nil
}
func (c *memoryCache) TakeTemporary(_ context.Context, key string) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	value := c.temporary[key]
	delete(c.temporary, key)
	return value, nil
}
func (c *memoryCache) PutTemporary(_ context.Context, key, value string, _ int) (bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, exists := c.temporary[key]; exists {
		return false, nil
	}
	c.temporary[key] = value
	return true, nil
}
func (c *memoryCache) SaveSession(_ context.Context, key string, values map[string]string, _ int) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sessions[key] = values
	return nil
}
func (c *memoryCache) Session(_ context.Context, key string) (map[string]string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.fail {
		return nil, errors.New("offline")
	}
	values := map[string]string{}
	for k, v := range c.sessions[key] {
		values[k] = v
	}
	return values, nil
}
func (c *memoryCache) Refresh(_ context.Context, key string, _ int) (bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.refreshes++
	return c.sessions[key] != nil, nil
}
func (c *memoryCache) RotationLock(_ context.Context, token string, _ int) (bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.lock != "" {
		return false, nil
	}
	c.lock = token
	return true, nil
}
func (c *memoryCache) RotationUnlock(_ context.Context, token string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.lock == token {
		c.lock = ""
	}
	return nil
}
func (c *memoryCache) ReplaceSessions(_ context.Context, current, keyHash string, _ int) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	for key := range c.sessions {
		if key != current {
			delete(c.sessions, key)
		}
	}
	clear(c.temporary)
	c.sessions[current]["key_hash"] = keyHash
	return nil
}

type auditRecorder struct {
	mu      sync.Mutex
	entries []store.AdminAudit
}

func (a *auditRecorder) AppendAudit(_ context.Context, e store.AdminAudit) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.entries = append(a.entries, e)
	return nil
}
func fixture(t *testing.T, cache Cache) (*Service, *auditRecorder, string) {
	t.Helper()
	dir := t.TempDir()
	key := "test-persistent-key-long-enough"
	if err := os.WriteFile(filepath.Join(dir, "admin_key"), []byte(key), 0600); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.LoadFrom(func(key string) string {
		if key == "SECRETS_DIR" {
			return dir
		}
		return ""
	})
	if err != nil {
		t.Fatal(err)
	}
	repo := &auditRecorder{}
	service, err := New(cfg, cache, repo)
	if err != nil {
		t.Fatal(err)
	}
	return service, repo, key
}
func expectStatus(t *testing.T, err error, status int) {
	t.Helper()
	var rejected *Error
	if !errors.As(err, &rejected) || rejected.Status != status {
		t.Fatalf("want status %d: %v", status, err)
	}
}

func TestAdminSessionCSRFPassiveAndFileRevocation(t *testing.T) {
	cache := newCache()
	s, _, key := fixture(t, cache)
	ctx := context.Background()
	info := RequestInfo{IP: "192.0.2.1"}
	session, err := s.Login(ctx, key, info)
	if err != nil {
		t.Fatal(err)
	}
	if len(session.Cookie) != 43 || len(session.CSRF) != 43 || session.Kind != "persistent" {
		t.Fatalf("session: %+v", session)
	}
	_, err = s.Authenticate(ctx, session.Cookie, "", "", "POST", "")
	expectStatus(t, err, 403)
	_, err = s.Authenticate(ctx, session.Cookie, session.CSRF, "wrong", "DELETE", "")
	expectStatus(t, err, 403)
	got, err := s.Authenticate(ctx, session.Cookie, session.CSRF, session.CSRF, "POST", "passive")
	if err != nil || got.Refresh || cache.refreshes != 0 {
		t.Fatalf("passive: %+v %v", got, err)
	}
	_, err = s.Authenticate(ctx, session.Cookie, session.CSRF, "", "GET", "")
	if err != nil || cache.refreshes != 1 {
		t.Fatalf("refresh: %v", err)
	}
	if err := os.WriteFile(filepath.Join(s.settings.SecretsDirectory, "admin_key"), []byte("changed-persistent-key"), 0600); err != nil {
		t.Fatal(err)
	}
	_, err = s.Authenticate(ctx, session.Cookie, session.CSRF, "", "GET", "")
	expectStatus(t, err, 401)
	if cache.sessions["admin:session:"+session.Hash] != nil {
		t.Fatal("stale session not removed")
	}
}

func TestAdminRateLimitAndRedisFailClosed(t *testing.T) {
	cache := newCache()
	s, _, key := fixture(t, cache)
	ctx := context.Background()
	info := RequestInfo{IP: "192.0.2.2"}
	for i := 0; i < 11; i++ {
		_, err := s.Login(ctx, "wrong", info)
		want := 403
		if i == 10 {
			want = 429
		}
		expectStatus(t, err, want)
	}
	_, err := s.Login(ctx, key, RequestInfo{IP: "192.0.2.3"})
	if err != nil {
		t.Fatal(err)
	}
	if cache.attempts["admin:fail:192.0.2.3"] != 0 {
		t.Fatal("success did not reset limiter")
	}
	cache.fail = true
	if _, err := s.Login(ctx, key, info); err == nil {
		t.Fatal("offline Redis accepted credentials")
	}
}

func TestTemporaryKeyOneUseAndPrivilegeBoundary(t *testing.T) {
	cache := newCache()
	s, _, key := fixture(t, cache)
	ctx := context.Background()
	owner, err := s.Login(ctx, key, RequestInfo{IP: "192.0.2.4"})
	if err != nil {
		t.Fatal(err)
	}
	token, err := s.TemporaryKey(ctx, owner, 15, RequestInfo{})
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	results := make(chan Session, 12)
	errs := make(chan error, 12)
	for range 12 {
		wg.Go(func() {
			session, err := s.Login(ctx, token, RequestInfo{IP: "192.0.2.5"})
			if err == nil {
				results <- session
			} else {
				errs <- err
			}
		})
	}
	wg.Wait()
	close(results)
	close(errs)
	count := 0
	var temporary Session
	for value := range results {
		count++
		temporary = value
	}
	if count != 1 || temporary.Kind != "temporary" || temporary.IdleTTL != 900 {
		t.Fatalf("redeemed %d sessions: %+v", count, temporary)
	}
	_, err = s.TemporaryKey(ctx, temporary, 15, RequestInfo{})
	expectStatus(t, err, 403)
	_, err = s.Rotate(ctx, temporary, "random", "", "", RequestInfo{})
	expectStatus(t, err, 403)
	_, err = s.TemporaryKey(ctx, owner, 17, RequestInfo{})
	expectStatus(t, err, 400)
}

func TestKeyRotationKeepsInitiatorAndRevokesOthers(t *testing.T) {
	cache := newCache()
	s, repo, key := fixture(t, cache)
	ctx := context.Background()
	info := RequestInfo{IP: "192.0.2.6"}
	first, err := s.Login(ctx, key, info)
	if err != nil {
		t.Fatal(err)
	}
	other, err := s.Login(ctx, key, info)
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.TemporaryKey(ctx, first, 15, info)
	if err != nil {
		t.Fatal(err)
	}
	newKey, err := s.Rotate(ctx, first, "random", "", "", info)
	if err != nil || len(newKey) != 64 {
		t.Fatalf("rotate: %v", err)
	}
	if len(cache.temporary) != 0 {
		t.Fatal("temporary keys survived rotation")
	}
	_, err = s.Authenticate(ctx, first.Cookie, first.CSRF, first.CSRF, "GET", "")
	if err != nil {
		t.Fatal("initiating session revoked", err)
	}
	_, err = s.Authenticate(ctx, other.Cookie, other.CSRF, "", "GET", "")
	expectStatus(t, err, 401)
	if len(repo.entries) < 5 {
		t.Fatal("audit evidence missing")
	}
	if err := s.Logout(ctx, first, info); err != nil {
		t.Fatal(err)
	}
	_, err = s.Authenticate(ctx, first.Cookie, "", "", "GET", "")
	expectStatus(t, err, 401)
}

func TestRedisCacheIntegration(t *testing.T) {
	url := os.Getenv("FRONTIERCLOUD_TEST_REDIS_URL")
	if url == "" {
		t.Skip("disposable Redis integration URL not configured")
	}
	opts, err := redis.ParseURL(url)
	if err != nil {
		t.Fatal(err)
	}
	client := redis.NewClient(opts)
	defer client.Close()
	cache := NewRedisCache(client)
	s, _, key := fixture(t, cache)
	ctx := context.Background()
	info := RequestInfo{IP: "192.0.2.200"}
	first, err := s.Login(ctx, key, info)
	if err != nil {
		t.Fatal(err)
	}
	defer cache.Delete(ctx, "admin:session:"+first.Hash)
	ttl, err := client.TTL(ctx, "admin:session:"+first.Hash).Result()
	if err != nil || ttl < time.Duration(s.settings.AdminSessionTTL-2)*time.Second {
		t.Fatalf("TTL: %v %v", ttl, err)
	}
	values, err := cache.Session(ctx, "admin:session:"+first.Hash)
	if err != nil || values["key_hash"] != hash(key) {
		t.Fatalf("hash format: %v %v", values, err)
	}
	temporary, err := s.TemporaryKey(ctx, first, 15, info)
	if err != nil {
		t.Fatal(err)
	}
	second, err := s.Login(ctx, temporary, info)
	if err != nil {
		t.Fatal(err)
	}
	defer cache.Delete(ctx, "admin:session:"+second.Hash)
	_, err = s.Login(ctx, temporary, info)
	expectStatus(t, err, 403)
	defer cache.Delete(ctx, "admin:fail:"+info.IP)
	_, err = s.Rotate(ctx, first, "random", "", "", info)
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.Authenticate(ctx, first.Cookie, first.CSRF, first.CSRF, "POST", "")
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.Authenticate(ctx, second.Cookie, second.CSRF, "", "GET", "")
	expectStatus(t, err, 401)
}
