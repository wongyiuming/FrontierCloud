package admin

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type Error struct {
	Status int
	Detail any
}

func (e *Error) Error() string            { return "admin request rejected" }
func reject(status int, detail any) error { return &Error{status, detail} }

type Service struct {
	settings   config.Config
	cache      Cache
	repository store.AdminRepository
}
type RequestInfo struct{ IP, UserAgent, RequestID, TraceID string }
type Session struct {
	Cookie  string
	CSRF    string
	Hash    string
	Kind    string
	IdleTTL int
	Refresh bool
}

func New(settings config.Config, cache Cache, repo store.AdminRepository) (*Service, error) {
	s := &Service{settings, cache, repo}
	if _, err := s.keyHash(); err != nil {
		return nil, err
	}
	return s, nil
}
func hash(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}
func equal(a, b string) bool { return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1 }
func randomToken(size int) (string, error) {
	bytes := make([]byte, size)
	_, err := rand.Read(bytes)
	return base64.RawURLEncoding.EncodeToString(bytes), err
}
func (s *Service) keyHash() (string, error) {
	path := filepath.Join(s.settings.SecretsDirectory, "admin_key")
	info, err := os.Lstat(path)
	if err != nil {
		return "", errors.New("Admin key was not initialized")
	}
	if !info.Mode().IsRegular() {
		return "", errors.New("Admin key must be a regular file")
	}
	bytes, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	value := strings.TrimSpace(string(bytes))
	if value == "" {
		return "", errors.New("Admin key is empty")
	}
	return hash(value), nil
}
func temporaryTTL(ttl int) bool { return ttl == 900 || ttl == 1800 || ttl == 3600 || ttl == 7200 }

func (s *Service) Audit(ctx context.Context, session, action, source, result, detail string, count int, info RequestInfo) error {
	return s.repository.AppendAudit(ctx, store.AdminAudit{SessionHash: session, Action: action, TargetCount: count, SourceSummary: source, Result: result, Detail: detail, ClientIP: info.IP, UserAgent: info.UserAgent, RequestID: info.RequestID, TraceID: info.TraceID})
}
func (s *Service) auditBestEffort(ctx context.Context, session, action, source, result, detail string, count int, info RequestInfo) {
	if err := s.Audit(ctx, session, action, source, result, detail, count, info); err != nil {
		slog.Error("admin audit write failed", "action", action, "result", result, "session_hash", session, "error", err)
	}
}

func (s *Service) Login(ctx context.Context, key string, info RequestInfo) (Session, error) {
	failed, err := s.cache.ReserveAttempt(ctx, "admin:fail:"+info.IP, s.settings.AdminFailedWindow)
	if err != nil {
		return Session{}, err
	}
	if failed > int64(s.settings.AdminMaxFailed) {
		s.auditBestEffort(ctx, "", "admin_login", "", "rate_limited", "", 0, info)
		return Session{}, reject(429, map[string]string{"code": "ADMIN_RATE_LIMITED", "message": "验证请求过于频繁，请稍后再试"})
	}
	persistent, err := s.keyHash()
	if err != nil {
		return Session{}, err
	}
	key = strings.TrimSpace(key)
	supplied := ""
	if utf8.RuneCountInString(key) >= 1 && utf8.RuneCountInString(key) <= 512 {
		supplied = hash(key)
	}
	kind, ttl := "persistent", s.settings.AdminSessionTTL
	accepted := supplied != "" && equal(supplied, persistent)
	if !accepted && supplied != "" {
		serialized, err := s.cache.TakeTemporary(ctx, "admin:temporary-key:"+supplied)
		if err != nil {
			return Session{}, err
		}
		var payload struct {
			KeyHash string `json:"key_hash"`
			IdleTTL int    `json:"idle_ttl"`
		}
		if serialized != "" && json.Unmarshal([]byte(serialized), &payload) == nil && temporaryTTL(payload.IdleTTL) && equal(payload.KeyHash, persistent) {
			accepted = true
			kind = "temporary"
			ttl = payload.IdleTTL
		}
	}
	if !accepted {
		s.auditBestEffort(ctx, "", "admin_login", "", "rejected", "invalid_key", 0, info)
		return Session{}, reject(403, map[string]string{"code": "ADMIN_KEY_INVALID", "message": "Admin Key 无效，请检查输入"})
	}
	if err := s.cache.Delete(ctx, "admin:fail:"+info.IP); err != nil {
		return Session{}, err
	}
	cookie, err := randomToken(32)
	if err != nil {
		return Session{}, err
	}
	csrf, err := randomToken(32)
	if err != nil {
		return Session{}, err
	}
	session := Session{Cookie: cookie, CSRF: csrf, Hash: hash(cookie), Kind: kind, IdleTTL: ttl, Refresh: true}
	if err := s.cache.SaveSession(ctx, "admin:session:"+session.Hash, map[string]string{"key_hash": persistent, "created_at": time.Now().UTC().Format("2006-01-02T15:04:05.000000"), "idle_ttl": strconv.Itoa(ttl), "credential_kind": kind}, ttl); err != nil {
		return Session{}, err
	}
	s.auditBestEffort(ctx, session.Hash, "admin_login", kind, "success", "idle_ttl="+strconv.Itoa(ttl), 1, info)
	return session, nil
}

func (s *Service) Authenticate(ctx context.Context, cookie, csrfCookie, csrfHeader, method, activity string) (Session, error) {
	if cookie == "" || len(cookie) > 512 {
		return Session{}, reject(401, "特权模式已失效，请重新登录")
	}
	key := "admin:session:" + hash(cookie)
	values, err := s.cache.Session(ctx, key)
	if err != nil {
		return Session{}, err
	}
	if len(values) == 0 {
		return Session{}, reject(401, "特权模式已失效，请重新登录")
	}
	if method == "POST" || method == "PUT" || method == "PATCH" || method == "DELETE" {
		if csrfCookie == "" || csrfHeader == "" || !equal(csrfCookie, csrfHeader) {
			return Session{}, reject(403, "CSRF 校验失败")
		}
	}
	persistent, err := s.keyHash()
	if err != nil {
		return Session{}, err
	}
	if !equal(values["key_hash"], persistent) {
		if err := s.cache.Delete(ctx, key); err != nil {
			return Session{}, err
		}
		return Session{}, reject(401, "Admin Key 已变更，请使用新 Key 重新登录")
	}
	kind, ttl := values["credential_kind"], s.settings.AdminSessionTTL
	if kind == "temporary" {
		ttl, _ = strconv.Atoi(values["idle_ttl"])
		if !temporaryTTL(ttl) {
			if err := s.cache.Delete(ctx, key); err != nil {
				return Session{}, err
			}
			return Session{}, reject(401, "临时特权会话无效，请重新登录")
		}
	} else {
		kind = "persistent"
	}
	refresh := strings.ToLower(strings.TrimSpace(activity)) != "passive"
	if refresh {
		alive, err := s.cache.Refresh(ctx, key, ttl)
		if err != nil {
			return Session{}, err
		}
		if !alive {
			return Session{}, reject(401, "特权模式已失效，请重新登录")
		}
	}
	return Session{Cookie: cookie, CSRF: csrfCookie, Hash: hash(cookie), Kind: kind, IdleTTL: ttl, Refresh: refresh}, nil
}

func (s *Service) TemporaryKey(ctx context.Context, session Session, minutes int, info RequestInfo) (string, error) {
	if session.Kind != "persistent" {
		return "", reject(403, "临时 Admin Key 会话不能继续签发临时 Key")
	}
	if !temporaryTTL(minutes * 60) {
		return "", reject(400, "临时 Admin Key 有效期必须为 15、30、60 或 120 分钟")
	}
	owner, err := s.keyHash()
	if err != nil {
		return "", err
	}
	payload, err := json.Marshal(map[string]any{"key_hash": owner, "idle_ttl": minutes * 60, "issued_by_session_hash": session.Hash, "created_at": time.Now().UTC().Format("2006-01-02T15:04:05.000000")})
	if err != nil {
		return "", err
	}
	for range 3 {
		token, err := randomToken(32)
		if err != nil {
			return "", err
		}
		stored, err := s.cache.PutTemporary(ctx, "admin:temporary-key:"+hash(token), string(payload), minutes*60)
		if err != nil {
			return "", err
		}
		if stored {
			s.auditBestEffort(ctx, session.Hash, "temporary_admin_key_issue", strconv.Itoa(minutes)+"m", "success", "single_use=true", 1, info)
			return token, nil
		}
	}
	return "", errors.New("failed to allocate temporary credential")
}

func (s *Service) Logout(ctx context.Context, session Session, info RequestInfo) error {
	if err := s.cache.Delete(ctx, "admin:session:"+session.Hash); err != nil {
		return err
	}
	s.auditBestEffort(ctx, session.Hash, "admin_logout", "", "success", "", 1, info)
	return nil
}

func replaceKey(directory, value string) error {
	f, err := os.CreateTemp(directory, ".admin_key-*.new")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if err = f.Chmod(0600); err == nil {
		_, err = f.WriteString(value + "\n")
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err := os.Rename(f.Name(), filepath.Join(directory, "admin_key")); err != nil {
		return err
	}
	if dir, err := os.Open(directory); err == nil {
		_ = dir.Sync()
		_ = dir.Close()
	}
	return nil
}

func (s *Service) Rotate(ctx context.Context, session Session, mode, custom, confirmation string, info RequestInfo) (string, error) {
	if session.Kind != "persistent" {
		return "", reject(403, "临时 Admin Key 会话不能修改长期 Admin Key")
	}
	var key string
	var err error
	if mode == "random" {
		key, err = randomToken(48)
		if err != nil {
			return "", err
		}
	} else if mode == "custom" {
		key = strings.TrimSpace(custom)
		size := utf8.RuneCountInString(key)
		if size < 16 || size > 512 {
			return "", reject(400, "自定义 Admin Key 长度必须为 16 到 512 个字符")
		}
		if !equal(key, confirmation) {
			return "", reject(400, "两次输入的 Admin Key 不一致")
		}
	} else {
		return "", reject(400, "Admin Key 生成模式无效")
	}
	lock, err := randomToken(32)
	if err != nil {
		return "", err
	}
	deadline, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		acquired, err := s.cache.RotationLock(deadline, lock, 120)
		if err != nil {
			return "", err
		}
		if acquired {
			break
		}
		select {
		case <-deadline.Done():
			return "", reject(409, "Admin Key 正在轮换，请稍后重试")
		case <-ticker.C:
		}
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if err := s.cache.RotationUnlock(cleanup, lock); err != nil {
			slog.Error("admin rotation lock release failed", "error", err)
		}
	}()
	current, err := s.keyHash()
	if err != nil {
		return "", err
	}
	// Recheck the initiating session AFTER obtaining the distributed lock. A
	// concurrent rotation can revoke this session while it waits for the lock.
	values, err := s.cache.Session(ctx, "admin:session:"+session.Hash)
	if err != nil {
		return "", err
	}
	if !equal(values["key_hash"], current) {
		return "", reject(401, "Admin Key 已变更，请重新登录")
	}
	if equal(hash(key), current) {
		return "", reject(400, "新 Admin Key 不能与当前 Key 相同")
	}
	if err := s.Audit(ctx, session.Hash, "admin_key_rotate", mode, "pending", "", 1, info); err != nil {
		return "", err
	}
	if err := replaceKey(s.settings.SecretsDirectory, key); err != nil {
		return "", err
	}
	// The file is authoritative. Return a newly published random key even when
	// Redis reconciliation fails; stale sessions fail key-hash verification.
	if err := s.cache.ReplaceSessions(ctx, "admin:session:"+session.Hash, hash(key), s.settings.AdminSessionTTL); err != nil {
		slog.Error("admin rotation session reconciliation failed", "error", err)
	}
	s.auditBestEffort(ctx, session.Hash, "admin_key_rotate", mode, "success", "", 1, info)
	return key, nil
}
