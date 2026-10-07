package httpapi

import (
	"log/slog"
	"regexp"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/network"
)

var requestIDPattern = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,128}$`)
var tracePattern = regexp.MustCompile(`^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$`)

func requests(resolver *network.Resolver) gin.HandlerFunc {
	return func(c *gin.Context) {
		started := time.Now()
		identity := resolver.Resolve(c.Request)
		id, err := randomUUID()
		if err != nil {
			detail(c, 500, "Internal server error")
			c.Abort()
			return
		}
		id = strings.ReplaceAll(id, "-", "")
		trace := id
		if supplied := c.GetHeader("X-Request-ID"); identity.FromTrustedProxy && requestIDPattern.MatchString(supplied) {
			id = supplied
		}
		if match := tracePattern.FindStringSubmatch(c.GetHeader("Traceparent")); identity.FromTrustedProxy && match != nil && match[1] != strings.Repeat("0", 32) && match[2] != strings.Repeat("0", 16) {
			trace = match[1]
		}
		c.Set("request_id", id)
		c.Set("trace_id", trace)
		c.Header("X-Request-ID", id)
		c.Header("X-Audit-Trace-ID", trace)
		c.Next()
		if c.Writer.Status() >= 400 || (c.Request.URL.Path != "/health" && c.Request.URL.Path != "/health/live" && c.Request.URL.Path != "/health/ready" && c.Request.URL.Path != "/api/v1/health" && c.Request.URL.Path != "/metrics") {
			slog.Info("request_completed", "method", c.Request.Method, "path", c.Request.URL.Path, "status", c.Writer.Status(), "duration_ms", float64(time.Since(started).Microseconds())/1000, "client_ip", identity.IP, "request_id", id, "trace_id", trace)
		}
	}
}
