package httpapi

import (
	"context"
	"errors"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/security"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func SecurityMiddleware(service *security.Service, resolver *network.Resolver) gin.HandlerFunc {
	return func(c *gin.Context) {
		identity := resolver.Resolve(c.Request)
		if identity.TrustedHeaderMissing {
			detail(c, 400, "Invalid proxy identity")
			c.Abort()
			return
		}
		lookup, cancel := context.WithTimeout(c.Request.Context(), 2*time.Second)
		ban, err := service.Block(lookup, identity.IP)
		cancel()
		if err != nil {
			c.Header("Cache-Control", "no-store")
			detail(c, 503, "API security policy unavailable")
			slog.Error("IP policy lookup failed", "error", err)
			c.Abort()
			return
		}
		if ban != nil {
			c.Header("Cache-Control", "no-store")
			c.JSON(403, gin.H{"detail": "Request blocked by API security policy", "expires_at": ban.ExpiresAt.UTC().Format("2006-01-02T15:04:05.999999")})
			c.Abort()
			return
		}
		c.Next()
		if c.Writer.Status() == 405 || c.Writer.Status() == 404 && c.FullPath() == "" {
			accounting, cancel := context.WithTimeout(context.WithoutCancel(c.Request.Context()), 2*time.Second)
			defer cancel()
			audit := store.AdminAudit{ClientIP: identity.IP, RequestID: c.GetString("request_id"), TraceID: c.GetString("trace_id")}
			if err := service.Invalid(accounting, identity.IP, c.Request.Method, c.Request.URL.Path, c.Request.UserAgent(), audit); err != nil {
				slog.Error("invalid API accounting failed", "client_ip", identity.IP, "error", err)
			}
		}
	}
}

func RegisterSecurityAdmin(router *gin.Engine, a *Admin, service *security.Service) {
	group := router.Group("/api/v1/media/admin/security", a.transport, a.authenticate)
	group.GET("/blocks", func(c *gin.Context) {
		f := store.SecurityFilter{IP: c.Query("ip"), MatchMode: c.DefaultQuery("match_mode", "exact"), Status: c.Query("status"), IPOrder: c.DefaultQuery("ip_order", "asc"), SortOrder: c.Query("sort_order"), Page: 1, PageSize: 100}
		if f.IPOrder != "asc" && f.IPOrder != "desc" {
			invalid(c, "query", "ip_order")
			return
		}
		if f.MatchMode != "exact" && f.MatchMode != "fuzzy" {
			invalid(c, "query", "match_mode")
			return
		}
		if f.SortOrder != "" && f.SortOrder != "ip_asc" && f.SortOrder != "ip_desc" && f.SortOrder != "last_attack_asc" && f.SortOrder != "last_attack_desc" {
			invalid(c, "query", "sort_order")
			return
		}
		if !securityPage(c, &f.Page, &f.PageSize) {
			return
		}
		ip, err := security.FilterIP(f.IP, f.MatchMode)
		if err != nil {
			detail(c, 400, "IP 查询参数无效")
			return
		}
		f.IP = ip
		value, f, err := service.Summary(c.Request.Context(), f)
		if err != nil {
			securityError(c, err)
			return
		}
		sortOrder := f.SortOrder
		if sortOrder == "" {
			sortOrder = "ip_" + f.IPOrder
		}
		legal := 0
		for _, route := range router.Routes() {
			if strings.HasPrefix(route.Path, "/api/v1/") && route.Method != "HEAD" {
				legal++
			}
		}
		c.Header("Cache-Control", "private, no-store")
		c.JSON(200, gin.H{"events": value.Events, "whitelist": value.Whitelist, "active_ban_count": value.ActiveCount, "whitelist_count": value.WhitelistCount, "pagination": gin.H{"page": f.Page, "page_size": f.PageSize, "total": value.Total, "pages": max(1, (value.Total+f.PageSize-1)/f.PageSize), "ip_order": f.IPOrder, "sort_order": sortOrder, "match_mode": f.MatchMode}, "threshold": a.settings.SecurityInvalidLimit, "window_seconds": a.settings.SecurityInvalidWindow, "ban_seconds": 86400, "second_offense_permanent": true, "legal_api_count": legal})
	})
	for path, action := range map[string]string{"unban": "unban", "reban": "reban", "permanent-ban": "permanent_ban", "whitelist": "whitelist", "whitelist/remove": "whitelist_remove"} {
		group.POST("/"+path, func(c *gin.Context) {
			var body struct {
				IP     string `json:"ip"`
				Reason string `json:"reason"`
				Note   string `json:"note"`
			}
			if !decodeAdmin(c, &body) {
				return
			}
			if _, err := network.Normalize(body.IP); err != nil {
				detail(c, 400, "IP 地址无效")
				return
			}
			note := body.Reason
			if action == "whitelist" {
				note = body.Note
			}
			ban, ip, err := service.Policy(c.Request.Context(), body.IP, action, note, a.mutationAudit(c, "security_"+action, []string{body.IP}))
			if err != nil {
				securityError(c, err)
				return
			}
			response := gin.H{"status": "ok", "ip": ip}
			if ban != nil {
				response["id"] = ban.ID
				if ban.Kind == "permanent" {
					response["permanent"] = true
				} else {
					response["expires_at"] = ban.ExpiresAt.UTC().Format("2006-01-02T15:04:05.000000")
				}
			}
			c.JSON(200, response)
		})
	}
}
func securityError(c *gin.Context, err error) {
	if errors.Is(err, store.ErrPolicy) || errors.Is(err, store.ErrQueryTimeout) {
		detail(c, 400, err.Error())
		return
	}
	internalError(c, err)
}
func securityPage(c *gin.Context, page, size *int) bool {
	for key, target := range map[string]*int{"page": page, "page_size": size} {
		if raw, ok := c.GetQuery(key); ok {
			parsed, err := strconv.Atoi(raw)
			if err != nil || parsed < 1 || key == "page_size" && parsed > 200 || key == "page" && parsed > 1000000 {
				invalid(c, "query", key)
				return false
			}
			*target = parsed
		}
	}
	return true
}
