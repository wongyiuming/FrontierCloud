package httpapi

import (
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestMetricsTokenHidingRotationAndRealCounters(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "metrics_token")
	if err := os.WriteFile(file, []byte("secret-metrics\n"), 0600); err != nil {
		t.Fatal(err)
	}
	router := New(pass, fail)
	router.GET("/metrics", func(c *gin.Context) { serveMetrics(c, dir) })
	router.GET("/probe/:id", func(c *gin.Context) { c.Status(204) })
	router.GET("/panic", func(c *gin.Context) { panic("fixture") })
	scrape := func(headers []string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("GET", "/metrics", nil)
		for _, v := range headers {
			r.Header.Add("Authorization", v)
		}
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		return w
	}
	for _, headers := range [][]string{nil, {"secret-metrics"}, {"bearer secret-metrics"}, {"Bearer wrong"}, {"Bearer secret-metrics", "Bearer secret-metrics"}} {
		if w := scrape(headers); w.Code != 404 || strings.Contains(w.Body.String(), "secret-metrics") {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	request(router, "GET", "/health/ready", "")
	request(router, "GET", "/probe/private-id?token=private-query", "")
	request(router, "GET", "/panic", "")
	for _, method := range []string{"ATTACK_ONE", "ATTACK_TWO"} {
		request(router, method, "/arbitrary-private-path", "")
	}
	w := scrape([]string{"Bearer secret-metrics"})
	if w.Code != 200 || !strings.Contains(w.Header().Get("Content-Type"), "text/plain") || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal(w.Code, w.Header(), w.Body.String())
	}
	body := w.Body.String()
	for _, want := range []string{`frontiercloud_http_requests_total{method="GET",route="/probe/{id}",status="204"} 1`, `frontiercloud_http_requests_total{method="OTHER",route="unmatched",status="404"} 2`, `frontiercloud_dependency_ready{dependency="database"} 1`, `frontiercloud_dependency_ready{dependency="redis"} 0`, `frontiercloud_http_exceptions_total{method="GET",route="/panic"} 1`, `frontiercloud_http_request_duration_seconds_bucket`, `go_goroutines`} {
		if !strings.Contains(body, want) {
			t.Fatal("missing metric", want, body)
		}
	}
	for _, private := range []string{"private-id", "private-query", "arbitrary-private-path", "secret-metrics", "ATTACK_ONE"} {
		if strings.Contains(body, private) {
			t.Fatal("unbounded/private label", private)
		}
	}
	if err := os.WriteFile(file, []byte("rotated-secret\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if w := scrape([]string{"Bearer secret-metrics"}); w.Code != 404 {
		t.Fatal("old token survived rotation")
	}
	if w := scrape([]string{"Bearer rotated-secret"}); w.Code != 200 {
		t.Fatal("new token unavailable")
	}
	if err := os.Remove(file); err != nil {
		t.Fatal(err)
	}
	if w := scrape([]string{"Bearer rotated-secret"}); w.Code != 404 {
		t.Fatal("missing token allowed scrape")
	}
}

func TestMetricsInProgressConcurrencyAndDependencyRecovery(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "metrics_token"), []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	router := New(pass, pass)
	router.GET("/metrics", func(c *gin.Context) { serveMetrics(c, dir) })
	entered, release := make(chan struct{}), make(chan struct{})
	router.GET("/blocked", func(c *gin.Context) { close(entered); <-release; c.Status(200) })
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); request(router, "GET", "/blocked", "") }()
	<-entered
	r := httptest.NewRequest("GET", "/metrics", nil)
	r.Header.Set("Authorization", "Bearer fixture")
	w := httptest.NewRecorder()
	router.ServeHTTP(w, r)
	close(release)
	wg.Wait()
	if !strings.Contains(w.Body.String(), `frontiercloud_http_requests_in_progress{method="GET"} 2`) {
		t.Fatal(w.Body.String())
	}
	request(router, "GET", "/health", "")
	w = httptest.NewRecorder()
	router.ServeHTTP(w, r)
	if !strings.Contains(w.Body.String(), `frontiercloud_dependency_ready{dependency="redis"} 1`) {
		t.Fatal("readiness not reflected")
	}
}

func TestMetricsTokenRejectsSymlinksAndOversize(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "metrics_token")
	if err := os.WriteFile(file, []byte(strings.Repeat("x", 4097)), 0600); err != nil {
		t.Fatal(err)
	}
	if metricsToken(dir) != "" {
		t.Fatal("oversize secret accepted")
	}
	if err := os.Remove(file); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(target, []byte("outside"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, file); err != nil {
		t.Skip("symlinks unavailable", err)
	}
	if metricsToken(dir) != "" {
		t.Fatal("symlink token accepted")
	}
}
