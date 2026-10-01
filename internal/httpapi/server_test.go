package httpapi

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func pass(context.Context) error { return nil }

func fail(context.Context) error { return errors.New("unavailable") }

func TestReadinessContract(t *testing.T) {
	for _, testCase := range []struct {
		name     string
		database Check
		redis    Check
		status   int
		body     string
	}{
		{name: "ready", database: pass, redis: pass, status: http.StatusOK, body: `"status":"ready"`},
		{name: "database unavailable", database: fail, redis: pass, status: http.StatusServiceUnavailable, body: `"database":"unavailable"`},
		{name: "redis unavailable", database: pass, redis: fail, status: http.StatusServiceUnavailable, body: `"redis":"unavailable"`},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			response := httptest.NewRecorder()
			request := httptest.NewRequest(http.MethodGet, "/health/ready", nil)
			New(testCase.database, testCase.redis).ServeHTTP(response, request)
			if response.Code != testCase.status || !strings.Contains(response.Body.String(), testCase.body) {
				t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
			}
			if response.Header().Get("Cache-Control") != "no-store" {
				t.Fatal("readiness response is cacheable")
			}
		})
	}
}

func TestAllHealthPathsAreAvailable(t *testing.T) {
	router := New(pass, pass)
	for _, path := range []string{"/health", "/health/ready", "/api/v1/health", "/health/live"} {
		response := httptest.NewRecorder()
		router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
		if response.Code != http.StatusOK {
			t.Fatalf("%s returned %d", path, response.Code)
		}
	}
}
