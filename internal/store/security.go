package store

import (
	"context"
	"errors"
	"time"
)

var ErrPolicy = errors.New("invalid security policy operation")
var ErrQueryTimeout = errors.New("模糊查询计算超过 250ms，请增加 IP 片段长度")

type IPBan struct {
	ID        int64
	IP        string
	Kind      string
	ExpiresAt time.Time
}
type SecurityFilter struct {
	IP, MatchMode, Status, IPOrder, SortOrder string
	Page, PageSize                            int
	Window                                    int
}
type SecurityItem struct {
	IP          string  `json:"ip"`
	BanCount    int64   `json:"ban_count"`
	AttackCount int64   `json:"attack_count"`
	LastAttack  *string `json:"last_attack_at"`
	Status      string  `json:"status"`
	Active      bool    `json:"active"`
	Permanent   bool    `json:"permanent"`
	Whitelisted bool    `json:"whitelisted"`
	Kind        *string `json:"ban_kind"`
	Reason      *string `json:"reason"`
}
type WhitelistItem struct {
	IP          string  `json:"ip"`
	Note        string  `json:"note"`
	AttackCount int64   `json:"attack_count"`
	LastAttack  *string `json:"last_attack_at"`
}
type SecuritySummary struct {
	Events         []SecurityItem  `json:"events"`
	Whitelist      []WhitelistItem `json:"whitelist"`
	ActiveCount    int             `json:"active_ban_count"`
	WhitelistCount int             `json:"whitelist_count"`
	Total          int             `json:"-"`
}
type EdgeBan struct {
	IP        string
	ExpiresAt time.Time
	Permanent bool
}
type SecurityRepository interface {
	IPBlock(context.Context, string) (*IPBan, error)
	RecordInvalidAPI(context.Context, string, string, string, string, int, int, AdminAudit) (int, error)
	SetIPPolicy(context.Context, string, string, string, AdminAudit) (*IPBan, error)
	SecuritySummary(context.Context, SecurityFilter) (SecuritySummary, error)
	EdgeBans(context.Context) ([]EdgeBan, int64, error)
	AcknowledgeEdge(context.Context, int64) error
	EdgeProjection(context.Context) (int64, int64, error)
}
