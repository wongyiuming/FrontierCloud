package release

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func verifierFixture(t *testing.T, handler http.HandlerFunc) *Verifier {
	t.Helper()
	server := httptest.NewTLSServer(handler)
	t.Cleanup(server.Close)
	v, err := NewVerifier(DefaultPolicy(), "private-test-token")
	if err != nil {
		t.Fatal(err)
	}
	v.base = server.URL
	v.client.Transport = server.Client().Transport
	return v
}

func TestVerificationRequiresUniqueReviewedTreeAndNewestExactPush(t *testing.T) {
	target, source, tree := strings.Repeat("a", 40), strings.Repeat("b", 40), strings.Repeat("c", 40)
	for _, kind := range []string{"success", "newest-failed", "tree-diff", "foreign-repo", "wrong-branch", "wrong-event", "wrong-commit", "ambiguous", "wrong-merge", "invalid-head"} {
		t.Run(kind, func(t *testing.T) {
			v := verifierFixture(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer private-test-token" {
					t.Error("missing scoped authentication")
				}
				w.Header().Set("Content-Type", "application/json")
				switch r.URL.Path {
				case "/branches/main":
					head := target
					if kind == "invalid-head" {
						head = "bad"
					}
					fmt.Fprintf(w, `{"commit":{"sha":%q,"commit":{"tree":{"sha":%q}}}}`, head, tree)
				case "/commits/" + target + "/pulls":
					repo, merge := "wongyiuming/FrontierCloud", target
					if kind == "foreign-repo" {
						repo = "foreign/repo"
					}
					if kind == "wrong-merge" {
						merge = source
					}
					p := map[string]any{"merged_at": "2026-10-02T00:00:00Z", "merge_commit_sha": merge, "base": map[string]any{"ref": "main"}, "head": map[string]any{"ref": "dev", "sha": source, "repo": map[string]any{"full_name": repo}}}
					pulls := []any{p}
					if kind == "ambiguous" {
						pulls = append(pulls, map[string]any{"merged_at": "2026-10-02T00:00:00Z", "merge_commit_sha": merge, "base": map[string]any{"ref": "main"}, "head": map[string]any{"ref": "dev", "sha": tree, "repo": map[string]any{"full_name": repo}}})
					}
					json.NewEncoder(w).Encode(pulls)
				case "/commits/" + source:
					reviewed := tree
					if kind == "tree-diff" {
						reviewed = target
					}
					fmt.Fprintf(w, `{"sha":%q,"commit":{"tree":{"sha":%q}}}`, source, reviewed)
				case "/actions/workflows/docker.yml/runs":
					if r.URL.Query().Get("event") != "push" || r.URL.Query().Get("head_sha") != source {
						t.Error("unscoped workflow lookup")
					}
					branch, event, sha := "dev", "push", source
					if kind == "wrong-branch" {
						branch = "main"
					}
					if kind == "wrong-event" {
						event = "workflow_dispatch"
					}
					if kind == "wrong-commit" {
						sha = target
					}
					newest := map[string]any{"head_branch": branch, "event": event, "head_sha": sha, "status": "completed", "conclusion": "success", "run_number": 9}
					if kind == "newest-failed" {
						newest["conclusion"] = "failure"
					}
					older := map[string]any{"head_branch": branch, "event": event, "head_sha": sha, "status": "completed", "conclusion": "success", "run_number": 8}
					json.NewEncoder(w).Encode(map[string]any{"workflow_runs": []any{newest, older}})
				default:
					t.Error("unexpected verification URL", r.URL)
					w.WriteHeader(404)
				}
			})
			result, err := v.Status(context.Background(), false)
			if err != nil || result["publishable"] != (kind == "success") {
				t.Fatal(kind, result, err)
			}
			if strings.Contains(fmt.Sprint(result), "private-test-token") {
				t.Fatal("token leaked into report")
			}
		})
	}
}

func TestVerificationCacheContextAndRateLimitBackoffFailClosed(t *testing.T) {
	var calls atomic.Int32
	var limited atomic.Bool
	v := verifierFixture(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if limited.Load() {
			w.Header().Set("X-RateLimit-Remaining", "0")
			w.Header().Set("Retry-After", "120")
			w.WriteHeader(403)
			return
		}
		fmt.Fprintf(w, `{"commit":{"sha":%q,"commit":{"tree":{"sha":%q}}}}`, strings.Repeat("a", 40), strings.Repeat("b", 40))
	})
	// A branch with no merged PR is valid evidence but never publishable.
	v.client.Transport = &fixtureTransport{base: v.client.Transport}
	now := time.Date(2026, 10, 2, 0, 0, 0, 0, time.UTC)
	v.now = func() time.Time { return now }
	var wg sync.WaitGroup
	for range 16 {
		wg.Go(func() {
			out, err := v.Status(context.Background(), true)
			if err != nil || out["publishable"] != false {
				t.Error(out, err)
			}
		})
	}
	wg.Wait()
	if calls.Load() != 1 {
		t.Fatal("concurrent verification wasted quota", calls.Load())
	}
	out, err := v.Status(context.Background(), true)
	if err != nil {
		t.Fatal(err)
	}
	out["publishable"] = true
	out, _ = v.Status(context.Background(), true)
	if out["publishable"] != false {
		t.Fatal("caller changed cached release evidence")
	}
	limited.Store(true)
	now = now.Add(time.Minute)
	out, err = v.Status(context.Background(), true)
	if err != nil || out["error_kind"] != "rate_limited" || out["publishable"] != false || out["last_verified"] == nil {
		t.Fatal(out, err)
	}
	before := calls.Load()
	now = now.Add(30 * time.Second)
	out, err = v.Status(context.Background(), true)
	if err != nil || calls.Load() != before || out["publishable"] != false {
		t.Fatal("refresh bypassed backoff", out, err)
	}
	v.lock <- struct{}{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err = v.Status(ctx, true); err == nil {
		t.Fatal("cache wait ignored cancellation")
	}
	<-v.lock
}

type fixtureTransport struct{ base http.RoundTripper }

func (f *fixtureTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if strings.Contains(r.URL.Path, "/pulls") {
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader("[]"))}, nil
	}
	return f.base.RoundTrip(r)
}

func TestVerificationRejectsRedirectAndOversizedJSON(t *testing.T) {
	for _, kind := range []string{"redirect", "oversize", "trailing"} {
		t.Run(kind, func(t *testing.T) {
			var calls atomic.Int32
			v := verifierFixture(t, func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				switch kind {
				case "redirect":
					http.Redirect(w, r, "https://invalid.test/credential-target", 302)
				case "oversize":
					fmt.Fprint(w, strings.Repeat(" ", 2*1024*1024+1))
				case "trailing":
					fmt.Fprint(w, `{} {}`)
				}
			})
			out, err := v.Status(context.Background(), false)
			if err != nil || out["publishable"] != false || out["available"] != false || calls.Load() != 1 {
				t.Fatal(out, err, calls.Load())
			}
		})
	}
}
