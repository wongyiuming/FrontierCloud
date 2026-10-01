package store

import (
	"context"
	"errors"
)

var ErrUserExists = errors.New("用户名已存在")
var ErrRegistrationLimit = errors.New("该公网 IP 今日注册次数已达上限")
var ErrUserMissing = errors.New("用户不存在")
var ErrUserBlocked = errors.New("账号已被封禁或删除")
var ErrAccountConflict = errors.New("账号状态已变化，请重新登录")
var ErrUserQuota = errors.New("新空间不能小于用户已使用空间")

type KaraokeUser struct {
	ID           string `json:"user_id"`
	Username     string `json:"username"`
	NameKey      string `json:"-"`
	PasswordHash string `json:"-"`
	Status       string `json:"status"`
	Quota        int64  `json:"quota_bytes"`
	Used         int64  `json:"used_bytes"`
	CreatedAt    int64  `json:"created_at"`
	UpdatedAt    int64  `json:"-"`
}
type KaraokeAudit struct {
	UserID, Action, Result, IP, RequestID, TraceID string
	Addresses                                      []string
	Detail                                         map[string]any
}
type RegistrationCounts struct{ Failures, Successes int }
type UserPage struct {
	Items              []KaraokeUser
	Page, Pages, Total int
}
type KaraokeRepository interface {
	UserByID(context.Context, string) (*KaraokeUser, error)
	UserByName(context.Context, string) (*KaraokeUser, error)
	RegistrationCounts(context.Context, string, string) (RegistrationCounts, error)
	RegisterUser(context.Context, KaraokeUser, string, string, KaraokeAudit) error
	RegistrationFailure(context.Context, string, string, KaraokeAudit) error
	AccountAudit(context.Context, KaraokeAudit) error
	ConfirmLogin(context.Context, string, string, KaraokeAudit) error
	ChangePassword(context.Context, string, string, string, KaraokeAudit) error
	ListUsers(context.Context, string, int, int) (UserPage, error)
	MutateUser(context.Context, string, string, int64, KaraokeAudit) error
	StageUserDeletion(context.Context, string, KaraokeAudit) (bool, error)
	FinishUserDeletion(context.Context, string, KaraokeAudit) (bool, error)
	DeletingUsers(context.Context, int) ([]string, error)
}
