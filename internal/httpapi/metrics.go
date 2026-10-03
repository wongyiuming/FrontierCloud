package httpapi

import (
	"crypto/subtle"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

const metricsContextKey = "frontiercloud_metrics"

type metrics struct {
	requests     *prometheus.CounterVec
	duration     *prometheus.HistogramVec
	inProgress   *prometheus.GaugeVec
	exceptions   *prometheus.CounterVec
	dependencies *prometheus.GaugeVec
	handler      http.Handler
}

func newMetrics() *metrics {
	m := &metrics{
		requests:     prometheus.NewCounterVec(prometheus.CounterOpts{Name: "frontiercloud_http_requests_total", Help: "HTTP requests handled by FrontierCloud"}, []string{"method", "route", "status"}),
		duration:     prometheus.NewHistogramVec(prometheus.HistogramOpts{Name: "frontiercloud_http_request_duration_seconds", Help: "FrontierCloud HTTP request duration", Buckets: []float64{0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30}}, []string{"method", "route"}),
		inProgress:   prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "frontiercloud_http_requests_in_progress", Help: "FrontierCloud HTTP requests currently in progress"}, []string{"method"}),
		exceptions:   prometheus.NewCounterVec(prometheus.CounterOpts{Name: "frontiercloud_http_exceptions_total", Help: "Unhandled FrontierCloud HTTP exceptions"}, []string{"method", "route"}),
		dependencies: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "frontiercloud_dependency_ready", Help: "Whether a required FrontierCloud dependency is ready"}, []string{"dependency"}),
	}
	r := prometheus.NewRegistry()
	r.MustRegister(m.requests, m.duration, m.inProgress, m.exceptions, m.dependencies, collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	m.dependencies.WithLabelValues("database").Set(0)
	m.dependencies.WithLabelValues("redis").Set(0)
	m.handler = promhttp.HandlerFor(r, promhttp.HandlerOpts{MaxRequestsInFlight: 2, Timeout: 5 * time.Second})
	return m
}
func metricMethod(method string) string {
	switch strings.ToUpper(method) {
	case "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT":
		return strings.ToUpper(method)
	}
	return "OTHER"
}
func metricRoute(c *gin.Context) string {
	path := c.FullPath()
	if path == "" {
		return "unmatched"
	}
	// Route templates only: never client-chosen paths, queries or identifiers.
	parts := strings.Split(path, "/")
	for i, part := range parts {
		if strings.HasPrefix(part, ":") || strings.HasPrefix(part, "*") {
			parts[i] = "{" + part[1:] + "}"
		}
	}
	return strings.Join(parts, "/")
}
func (m *metrics) instrument(c *gin.Context) {
	c.Set(metricsContextKey, m)
	method := metricMethod(c.Request.Method)
	started := time.Now()
	m.inProgress.WithLabelValues(method).Inc()
	defer func() {
		m.inProgress.WithLabelValues(method).Dec()
		route := metricRoute(c)
		m.requests.WithLabelValues(method, route, strconv.Itoa(c.Writer.Status())).Inc()
		m.duration.WithLabelValues(method, route).Observe(time.Since(started).Seconds())
	}()
	c.Next()
}
func (m *metrics) recovered(c *gin.Context, _ any) {
	m.exceptions.WithLabelValues(metricMethod(c.Request.Method), metricRoute(c)).Inc()
	c.AbortWithStatus(http.StatusInternalServerError)
}
func metricsToken(directory string) string {
	root, err := os.OpenRoot(directory)
	if err != nil {
		return ""
	}
	defer root.Close()
	info, err := root.Lstat("metrics_token")
	if err != nil || !info.Mode().IsRegular() || info.Size() > 4096 {
		return ""
	}
	f, err := root.Open("metrics_token")
	if err != nil {
		return ""
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return ""
	}
	b, err := io.ReadAll(io.LimitReader(f, 4097))
	if err != nil || len(b) > 4096 {
		return ""
	}
	return strings.TrimSpace(string(b))
}
func serveMetrics(c *gin.Context, directory string) {
	c.Header("Cache-Control", "no-store")
	configured := metricsToken(directory)
	authorizations := c.Request.Header.Values("Authorization")
	supplied := ""
	if len(authorizations) == 1 && strings.HasPrefix(authorizations[0], "Bearer ") {
		supplied = strings.TrimPrefix(authorizations[0], "Bearer ")
	}
	if configured == "" || supplied == "" || subtle.ConstantTimeCompare([]byte(configured), []byte(supplied)) != 1 {
		detail(c, 404, "Not found")
		return
	}
	v, found := c.Get(metricsContextKey)
	m, ok := v.(*metrics)
	if !found || !ok {
		detail(c, 503, "Metrics unavailable")
		return
	}
	m.handler.ServeHTTP(c.Writer, c.Request)
}
