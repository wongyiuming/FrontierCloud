package karaoke

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"net/netip"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
)

const DefaultQuota = 200 * 1024 * 1024
const dummyHash = "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA$pBvZM-z6uzE8Uh2oYD3DkPSoiN84wwWjaC8fzHz1HF8"

var challengeID = regexp.MustCompile(`^[a-f0-9]{32}$`)
var sessionToken = regexp.MustCompile(`^[a-zA-Z0-9_-]{43}$`)

type Error struct {
	Status  int
	Detail  string
	Captcha bool
}

func (e *Error) Error() string { return e.Detail }
func unavailable() error {
	return &Error{Status: 503, Detail: "账号服务暂不可用，请稍后重试"}
}

type Service struct {
	repo              store.KaraokeRepository
	nodes             store.NodeRepository
	cache             Cache
	recoverRecordings func(context.Context, string) error
}

func (s *Service) ConfigureRecordings(recover func(context.Context, string) error) {
	s.recoverRecordings = recover
}

type Session struct{ Token, CSRF string }
type RequestInfo struct {
	IP, RequestID, TraceID string
	Addresses              []string
}

func New(repo store.KaraokeRepository, nodes store.NodeRepository, cache Cache) *Service {
	return &Service{repo: repo, nodes: nodes, cache: cache}
}
func (s *Service) RequireMaster(ctx context.Context) error {
	n, err := s.nodes.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	if n.Role != "Master" {
		return &Error{Status: 409, Detail: "账号与录音管理仅由 Master 提供"}
	}
	return nil
}
func randomToken(size int) (string, error) {
	value := make([]byte, size)
	_, err := rand.Read(value)
	return protocol.Encode(value), err
}
func hash(value string) string { h := sha256.Sum256([]byte(value)); return hex.EncodeToString(h[:]) }
func (s *Service) createSession(ctx context.Context, user store.KaraokeUser) (Session, error) {
	if s.cache == nil {
		return Session{}, unavailable()
	}
	token, err := randomToken(32)
	if err != nil {
		return Session{}, err
	}
	csrf, err := randomToken(24)
	if err != nil {
		return Session{}, err
	}
	if err := s.cache.SaveSession(ctx, sessionPrefix+hash(token), user.ID, csrf, hash(user.PasswordHash)); err != nil {
		return Session{}, unavailable()
	}
	return Session{token, csrf}, nil
}
func publicError(err error) error {
	switch {
	case errors.Is(err, store.ErrUserExists):
		return &Error{Status: 400, Detail: err.Error()}
	case errors.Is(err, store.ErrRegistrationLimit):
		return &Error{Status: 429, Detail: err.Error()}
	case errors.Is(err, store.ErrUserMissing):
		return &Error{Status: 404, Detail: err.Error()}
	case errors.Is(err, store.ErrUserBlocked):
		return &Error{Status: 403, Detail: err.Error()}
	case errors.Is(err, store.ErrUserQuota), errors.Is(err, store.ErrAccountConflict), errors.Is(err, store.ErrNodeState):
		return &Error{Status: 409, Detail: err.Error()}
	default:
		return err
	}
}
func normalizeAddresses(values []string) ([]string, error) {
	if len(values) > 8 {
		return nil, &Error{Status: 400, Detail: "网络地址过多"}
	}
	unique := map[string]bool{}
	result := []string{}
	for _, raw := range values {
		ip, err := network.Normalize(raw)
		if err != nil {
			return nil, &Error{Status: 400, Detail: "网络地址无效"}
		}
		addr, _ := netip.ParseAddr(ip)
		if addr.IsUnspecified() || addr.IsMulticast() {
			return nil, &Error{Status: 400, Detail: "网络地址无效"}
		}
		if !unique[ip] {
			result = append(result, ip)
			unique[ip] = true
		}
	}
	sort.Strings(result)
	return result, nil
}
func audit(info RequestInfo, action, result string) store.KaraokeAudit {
	return store.KaraokeAudit{Action: action, Result: result, IP: info.IP, RequestID: info.RequestID, TraceID: info.TraceID, Addresses: info.Addresses}
}
func registrationDay() string {
	return time.Now().In(time.FixedZone("Asia/Shanghai", 8*3600)).Format("20060102")
}

func (s *Service) Captcha(ctx context.Context) (string, error) {
	if s.cache == nil {
		return "", unavailable()
	}
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	id := hex.EncodeToString(value)
	const alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
	answer := make([]byte, 0, 5)
	for len(answer) < 5 {
		b := make([]byte, 1)
		if _, err := rand.Read(b); err != nil {
			return "", err
		}
		if int(b[0]) < 256-256%len(alphabet) {
			answer = append(answer, alphabet[int(b[0])%len(alphabet)])
		}
	}
	if err := s.cache.Captcha(ctx, id, hash(string(answer)), string(answer)); err != nil {
		return "", unavailable()
	}
	return id, nil
}
func (s *Service) CaptchaSVG(ctx context.Context, id string) (string, error) {
	if !challengeID.MatchString(id) {
		return "", &Error{Status: 404, Detail: "验证码已过期"}
	}
	if s.cache == nil {
		return "", unavailable()
	}
	answer, err := s.cache.CaptchaImage(ctx, id)
	if err != nil {
		return "", unavailable()
	}
	if len(answer) != 5 || strings.ContainsAny(answer, "<>&\"'") {
		return "", &Error{Status: 404, Detail: "验证码已过期"}
	}
	letters := ""
	for i, r := range answer {
		letters += fmt.Sprintf(`<text x="%d" y="40" transform="rotate(%d %d 40)">%c</text>`, 18+i*27, -8+i*4, 18+i*27, r)
	}
	return `<svg xmlns="http://www.w3.org/2000/svg" width="160" height="56" viewBox="0 0 160 56"><rect width="160" height="56" rx="10" fill="#101827"/><path d="M4 43L156 13M8 16L151 47" stroke="#44617d" stroke-width="2"/><g fill="#8be3ff" font-family="monospace" font-size="28" font-weight="700">` + letters + `</g></svg>`, nil
}
func (s *Service) consumeCaptcha(ctx context.Context, id, answer string) (bool, error) {
	if !challengeID.MatchString(id) || answer == "" {
		return false, nil
	}
	if s.cache == nil {
		return false, unavailable()
	}
	want, err := s.cache.TakeCaptcha(ctx, id)
	if err != nil {
		return false, unavailable()
	}
	return want != "" && subtle.ConstantTimeCompare([]byte(want), []byte(hash(strings.ToUpper(pythonStrip(answer))))) == 1, nil
}
func (s *Service) Register(ctx context.Context, username, password, challenge, captcha string, info RequestInfo) (store.KaraokeUser, Session, error) {
	if err := s.RequireMaster(ctx); err != nil {
		return store.KaraokeUser{}, Session{}, err
	}
	addresses, err := normalizeAddresses(info.Addresses)
	if err != nil {
		return store.KaraokeUser{}, Session{}, err
	}
	info.Addresses = addresses
	day := registrationDay()
	counts, err := s.repo.RegistrationCounts(ctx, info.IP, day)
	if err != nil {
		return store.KaraokeUser{}, Session{}, err
	}
	if counts.Failures >= 20 || counts.Successes >= 3 {
		return store.KaraokeUser{}, Session{}, publicError(store.ErrRegistrationLimit)
	}
	failure := func(err error) (store.KaraokeUser, Session, error) {
		a := audit(info, "register", "failure")
		a.Detail = map[string]any{"reason": "validation-or-duplicate"}
		if auditErr := s.repo.RegistrationFailure(ctx, info.IP, day, a); auditErr != nil {
			return store.KaraokeUser{}, Session{}, auditErr
		}
		return store.KaraokeUser{}, Session{}, err
	}
	valid, err := s.consumeCaptcha(ctx, challenge, captcha)
	if err != nil {
		return store.KaraokeUser{}, Session{}, err
	}
	if !valid {
		return failure(&Error{Status: 400, Detail: "验证码无效或已过期"})
	}
	name, key, err := NormalizeUsername(username)
	if err != nil {
		return failure(&Error{Status: 400, Detail: err.Error()})
	}
	if err := ValidatePassword(password); err != nil {
		return failure(&Error{Status: 400, Detail: err.Error()})
	}
	encoded, err := HashPassword(ctx, password)
	if err != nil {
		return store.KaraokeUser{}, Session{}, err
	}
	idBytes := make([]byte, 16)
	if _, err := rand.Read(idBytes); err != nil {
		return store.KaraokeUser{}, Session{}, err
	}
	user := store.KaraokeUser{ID: hex.EncodeToString(idBytes), Username: name, NameKey: key, PasswordHash: encoded, Status: "active", Quota: DefaultQuota, CreatedAt: time.Now().Unix()}
	if err := s.repo.RegisterUser(ctx, user, info.IP, day, audit(info, "register", "success")); err != nil {
		if errors.Is(err, store.ErrUserExists) {
			return failure(publicError(err))
		}
		return store.KaraokeUser{}, Session{}, publicError(err)
	}
	session, err := s.createSession(ctx, user)
	return user, session, err
}
func (s *Service) Login(ctx context.Context, username, password, challenge, captcha string, info RequestInfo) (store.KaraokeUser, Session, error) {
	if err := s.RequireMaster(ctx); err != nil {
		return store.KaraokeUser{}, Session{}, err
	}
	if s.cache == nil {
		return store.KaraokeUser{}, Session{}, unavailable()
	}
	addresses, err := normalizeAddresses(info.Addresses)
	if err != nil {
		return store.KaraokeUser{}, Session{}, err
	}
	info.Addresses = addresses
	_, key, err := NormalizeUsername(username)
	if err != nil {
		key = cases.Fold().String(norm.NFKC.String(username))
		runes := []rune(key)
		if len(runes) > 128 {
			key = string(runes[:128])
		}
	}
	failureKey := "karaoke:login-fail:" + hash(info.IP+":"+key)
	count, err := s.cache.Counter(ctx, failureKey)
	if err != nil {
		return store.KaraokeUser{}, Session{}, unavailable()
	}
	captchaVerified := false
	if count >= 3 {
		valid, err := s.consumeCaptcha(ctx, challenge, captcha)
		if err != nil {
			return store.KaraokeUser{}, Session{}, err
		}
		if !valid {
			return store.KaraokeUser{}, Session{}, &Error{Status: 400, Detail: "第 4 次登录起必须完成验证码", Captcha: true}
		}
		captchaVerified = true
	}
	count, err = s.cache.ReserveLogin(ctx, failureKey, captchaVerified)
	if err != nil {
		return store.KaraokeUser{}, Session{}, unavailable()
	}
	if count < 0 {
		return store.KaraokeUser{}, Session{}, &Error{Status: 400, Detail: "第 4 次登录起必须完成验证码", Captcha: true}
	}
	user, err := s.repo.UserByName(ctx, key)
	if err != nil {
		return store.KaraokeUser{}, Session{}, err
	}
	encoded := dummyHash
	if user != nil {
		encoded = user.PasswordHash
	}
	verified, err := VerifyPassword(ctx, password, encoded)
	if err != nil {
		return store.KaraokeUser{}, Session{}, err
	}
	if !verified || user == nil {
		a := audit(info, "login", "failure")
		if user != nil {
			a.UserID = user.ID
		}
		a.Detail = map[string]any{"failures": count}
		if err := s.repo.AccountAudit(ctx, a); err != nil {
			return store.KaraokeUser{}, Session{}, err
		}
		return store.KaraokeUser{}, Session{}, &Error{Status: 401, Detail: "用户名或密码错误", Captcha: count >= 3}
	}
	if err := s.repo.ConfirmLogin(ctx, user.ID, user.PasswordHash, audit(info, "login", "success")); err != nil {
		if errors.Is(err, store.ErrUserBlocked) {
			a := audit(info, "login", "blocked")
			a.UserID = user.ID
			if auditErr := s.repo.AccountAudit(ctx, a); auditErr != nil {
				return store.KaraokeUser{}, Session{}, auditErr
			}
		}
		return store.KaraokeUser{}, Session{}, publicError(err)
	}
	if err := s.cache.Delete(ctx, failureKey); err != nil {
		return store.KaraokeUser{}, Session{}, unavailable()
	}
	session, err := s.createSession(ctx, *user)
	return *user, session, err
}
func (s *Service) CurrentUser(ctx context.Context, token, cookieCSRF, supplied string, mutation, optional bool) (*store.KaraokeUser, error) {
	if err := s.RequireMaster(ctx); err != nil {
		return nil, err
	}
	unauth := func(detail string) (*store.KaraokeUser, error) {
		if optional {
			return nil, nil
		}
		return nil, &Error{Status: 401, Detail: detail}
	}
	if !sessionToken.MatchString(token) {
		return unauth("请先登录 卡拉OK账号")
	}
	if s.cache == nil {
		return nil, unavailable()
	}
	key := sessionPrefix + hash(token)
	value, err := s.cache.Session(ctx, key)
	if err != nil {
		return nil, unavailable()
	}
	if value["user_id"] == "" || value["password_fp"] == "" || value["generation"] == "" {
		return unauth("卡拉OK登录已失效")
	}
	if mutation && (cookieCSRF == "" || supplied == "" || subtle.ConstantTimeCompare([]byte(cookieCSRF), []byte(supplied)) != 1 || subtle.ConstantTimeCompare([]byte(cookieCSRF), []byte(value["csrf"])) != 1) {
		return nil, &Error{Status: 403, Detail: "卡拉OK请求校验失败"}
	}
	user, err := s.repo.UserByID(ctx, value["user_id"])
	if err != nil {
		return nil, err
	}
	if user == nil || user.Status != "active" {
		if err := s.cache.Delete(ctx, key); err != nil {
			return nil, unavailable()
		}
		if optional {
			return nil, nil
		}
		return nil, publicError(store.ErrUserBlocked)
	}
	valid, err := s.cache.UseSession(ctx, key, user.ID, value["csrf"], hash(user.PasswordHash))
	if err != nil {
		return nil, unavailable()
	}
	if !valid {
		return unauth("卡拉OK登录已失效")
	}
	return user, nil
}
func (s *Service) Logout(ctx context.Context, user store.KaraokeUser, token string, info RequestInfo) error {
	if s.cache == nil {
		return unavailable()
	}
	if err := s.cache.Delete(ctx, sessionPrefix+hash(token)); err != nil {
		return unavailable()
	}
	a := audit(info, "logout", "success")
	a.UserID = user.ID
	return s.repo.AccountAudit(ctx, a)
}
func (s *Service) Password(ctx context.Context, user store.KaraokeUser, current, next string, info RequestInfo) error {
	valid, err := VerifyPassword(ctx, current, user.PasswordHash)
	if err != nil {
		return err
	}
	if !valid {
		a := audit(info, "password-change", "failure")
		a.UserID = user.ID
		if err := s.repo.AccountAudit(ctx, a); err != nil {
			return err
		}
		return &Error{Status: 400, Detail: "当前密码错误"}
	}
	if err := ValidatePassword(next); err != nil {
		a := audit(info, "password-change", "failure")
		a.UserID = user.ID
		a.Detail = map[string]any{"reason": "password-policy"}
		if auditErr := s.repo.AccountAudit(ctx, a); auditErr != nil {
			return auditErr
		}
		return &Error{Status: 400, Detail: err.Error()}
	}
	encoded, err := HashPassword(ctx, next)
	if err != nil {
		return err
	}
	if s.cache == nil {
		return unavailable()
	}
	if err := s.cache.RevokeUser(ctx, user.ID); err != nil {
		return unavailable()
	}
	return publicError(s.repo.ChangePassword(ctx, user.ID, user.PasswordHash, encoded, audit(info, "password-change", "success")))
}
func (s *Service) ListUsers(ctx context.Context, q string, page, size int) (store.UserPage, error) {
	if err := s.RequireMaster(ctx); err != nil {
		return store.UserPage{}, err
	}
	return s.repo.ListUsers(ctx, q, page, size)
}
func (s *Service) MutateUser(ctx context.Context, id, action string, quota int64, info RequestInfo, actor string) error {
	if err := s.RequireMaster(ctx); err != nil {
		return err
	}
	if !challengeID.MatchString(id) {
		return publicError(store.ErrUserMissing)
	}
	if action == "ban" || action == "unban" {
		if s.cache == nil {
			return unavailable()
		}
		if err := s.cache.RevokeUser(ctx, id); err != nil {
			return unavailable()
		}
	}
	a := audit(info, "admin-user-"+action, "success")
	a.Detail = map[string]any{"actor": actor}
	return publicError(s.repo.MutateUser(ctx, id, action, quota, a))
}

func (s *Service) DeleteUser(ctx context.Context, id string, info RequestInfo, actor string) (bool, bool, error) {
	if err := s.RequireMaster(ctx); err != nil {
		return false, false, err
	}
	if !challengeID.MatchString(id) {
		return false, false, publicError(store.ErrUserMissing)
	}
	if s.cache == nil {
		return false, false, unavailable()
	}
	if err := s.cache.RevokeUser(ctx, id); err != nil {
		return false, false, unavailable()
	}
	a := audit(info, "account-delete", "pending")
	if actor != "" {
		a.Action = "admin-user-delete"
		a.Detail = map[string]any{"actor": actor}
	}
	found, err := s.repo.StageUserDeletion(ctx, id, a)
	if err != nil || !found {
		return found, !found, publicError(err)
	}
	if s.recoverRecordings != nil {
		if err := s.recoverRecordings(ctx, id); err != nil {
			return true, false, nil
		}
	}
	complete, err := s.repo.FinishUserDeletion(ctx, id, a)
	return true, complete, publicError(err)
}
