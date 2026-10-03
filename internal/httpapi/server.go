// Package httpapi exposes runtime-agnostic HTTP contracts through Gin.
package httpapi

import (
	"context"
	"net/http"
	"os"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/network"
)

const dependencyTimeout = 2 * time.Second

// Check performs one bounded readiness operation.
type Check func(context.Context) error

// New returns the initial Gin runtime surface. Public and Admin routes will be
// added only with matching contract tests against the Python implementation.
func New(database, redis Check) *gin.Engine {
	resolver, _ := network.New([]string{"172.16.0.0/12"})
	return NewWithResolver(database, redis, resolver)
}

func NewWithResolver(database, redis Check, resolver *network.Resolver, middleware ...gin.HandlerFunc) *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	m := newMetrics()
	router.Use(m.instrument, requests(resolver), gin.CustomRecoveryWithWriter(os.Stderr, m.recovered))
	router.Use(middleware...)
	router.HandleMethodNotAllowed = true
	router.NoRoute(func(c *gin.Context) { detail(c, 404, "Not Found") })
	router.NoMethod(func(c *gin.Context) { detail(c, 405, "Method Not Allowed") })
	router.GET("/health/live", func(ctx *gin.Context) {
		ctx.JSON(http.StatusOK, gin.H{
			"status":    "healthy",
			"timestamp": time.Now().UTC().Format("2006-01-02T15:04:05.000000+00:00"),
		})
	})
	ready := readiness(database, redis)
	router.GET("/health", ready)
	router.GET("/health/ready", ready)
	router.GET("/api/v1/health", ready)
	return router
}

func readiness(database, redis Check) gin.HandlerFunc {
	type result struct {
		name string
		err  error
	}
	return func(ctx *gin.Context) {
		requestContext, cancel := context.WithTimeout(ctx.Request.Context(), dependencyTimeout)
		defer cancel()
		results := make(chan result, 2)
		go func() { results <- result{name: "database", err: database(requestContext)} }()
		go func() { results <- result{name: "redis", err: redis(requestContext)} }()
		checks := map[string]string{"database": "unavailable", "redis": "unavailable"}
	collect:
		for range 2 {
			var value result
			select {
			case value = <-results:
			case <-requestContext.Done():
				break collect
			}
			if value.err == nil {
				checks[value.name] = "ready"
			} else {
				checks[value.name] = "unavailable"
			}
		}
		status, code := "ready", http.StatusOK
		if checks["database"] != "ready" || checks["redis"] != "ready" {
			status, code = "unavailable", http.StatusServiceUnavailable
		}
		if value, ok := ctx.Get(metricsContextKey); ok {
			m := value.(*metrics)
			for name, state := range checks {
				ready := float64(0)
				if state == "ready" {
					ready = 1
				}
				m.dependencies.WithLabelValues(name).Set(ready)
			}
		}
		ctx.Header("Cache-Control", "no-store")
		ctx.JSON(code, gin.H{"status": status, "checks": checks})
	}
}
