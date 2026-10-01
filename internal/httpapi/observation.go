package httpapi

import (
	"errors"
	"net/http"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/observation"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func RegisterObservations(router *gin.Engine, a *Admin, service *observation.Service, resolver *network.Resolver) {
	router.POST("/api/v1/media/network-observation", func(c *gin.Context) {
		var body struct {
			Addresses []string `json:"addresses"`
			Failure   string   `json:"failure"`
		}
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 16*1024)
		if !decodeAdmin(c, &body) {
			return
		}
		if len(body.Addresses) > 8 || len(body.Failure) > 32 {
			detail(c, 400, "Invalid WebRTC observation")
			return
		}
		identity := resolver.Resolve(c.Request)
		if identity.TrustedHeaderMissing {
			detail(c, 400, "Invalid proxy identity")
			return
		}
		value, err := service.Record(c.Request.Context(), identity.IP, body.Addresses, body.Failure)
		if errors.Is(err, observation.ErrRateLimit) {
			detail(c, 429, err.Error())
			return
		}
		if errors.Is(err, observation.ErrObservation) {
			detail(c, 400, err.Error())
			return
		}
		if err != nil {
			internalError(c, err)
			return
		}
		c.Header("Cache-Control", "no-store")
		c.JSON(200, value)
	})
	group := router.Group("/api/v1/media/admin/network", a.transport, a.authenticate)
	group.GET("/observations", func(c *gin.Context) {
		f := store.ObservationFilter{PublicIP: c.Query("public_ip"), WebRTCIP: c.Query("webrtc_ip"), MatchMode: c.DefaultQuery("match_mode", "exact"), View: c.DefaultQuery("view", "pairs"), Page: 1, PageSize: 100}
		if f.View != "pairs" && f.View != "public" && f.View != "webrtc" || f.MatchMode != "exact" && f.MatchMode != "fuzzy" {
			invalid(c, "query", "view")
			return
		}
		if utf8.RuneCountInString(f.PublicIP) > 45 || utf8.RuneCountInString(f.WebRTCIP) > 45 {
			invalid(c, "query", "ip")
			return
		}
		if !securityPage(c, &f.Page, &f.PageSize) {
			return
		}
		result, f, err := service.List(c.Request.Context(), f)
		if errors.Is(err, observation.ErrObservation) {
			detail(c, 400, "IP 查询参数无效")
			return
		}
		if err != nil {
			securityError(c, err)
			return
		}
		response := gin.H{"pagination": gin.H{"page": f.Page, "page_size": f.PageSize, "pages": max(1, (result.Total+f.PageSize-1)/f.PageSize), "total": result.Total}, "filters": gin.H{"public_ip": optionalFilter(f.PublicIP), "webrtc_ip": optionalFilter(f.WebRTCIP), "match_mode": f.MatchMode}, "view": f.View}
		if f.View == "pairs" {
			response["items"] = result.Items
		} else {
			response["groups"] = result.Groups
		}
		c.Header("Cache-Control", "private, no-store")
		c.JSON(200, response)
	})
}
func optionalFilter(value string) any {
	if value == "" {
		return nil
	}
	return value
}
