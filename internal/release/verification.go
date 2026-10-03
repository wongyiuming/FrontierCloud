package release

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

var fullSHA = regexp.MustCompile(`^[a-f0-9]{40}$`)

func ValidSHA(value string) bool { return fullSHA.MatchString(value) }
func Busy(value any) bool {
	switch value {
	case "queued", "running", "distributing", "restarting":
		return true
	}
	return false
}

type Policy struct{ Branch, Source string }

func DefaultPolicy() Policy  { return Policy{"main", "dev"} }
func (p Policy) Valid() bool { return p == DefaultPolicy() || p == (Policy{"gin_main", "gin_dev"}) }

// Cache/backoff are per verifier, context-aware and serialized. Responses are
// copied before they leave the cache; callers cannot mutate release evidence.
type Verifier struct {
	policy           Policy
	token            string
	base             string
	client           *http.Client
	lock             chan struct{}
	now              func() time.Time
	cache, last      map[string]any
	checked, backoff time.Time
}

func NewVerifier(policy Policy, token string) (*Verifier, error) {
	if !policy.Valid() {
		return nil, errors.New("invalid release branch policy")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.MaxConnsPerHost = 2
	transport.ResponseHeaderTimeout = 5 * time.Second
	return &Verifier{policy: policy, token: strings.TrimSpace(token), base: "https://api.github.com/repos/wongyiuming/FrontierCloud", client: &http.Client{Transport: transport, Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, lock: make(chan struct{}, 1), now: time.Now}, nil
}
func copyValue(value map[string]any) map[string]any {
	if value == nil {
		return nil
	}
	raw, _ := json.Marshal(value)
	var out map[string]any
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	_ = d.Decode(&out)
	return out
}

type apiFailure struct {
	kind   string
	status int
	retry  int64
	rate   map[string]any
}

func (f *apiFailure) Error() string { return "release verification unavailable" }
func rateInfo(h http.Header) map[string]any {
	value := map[string]any{}
	for name, key := range map[string]string{"X-RateLimit-Limit": "limit", "X-RateLimit-Remaining": "remaining", "X-RateLimit-Used": "used", "X-RateLimit-Reset": "reset_at"} {
		if n, err := strconv.ParseInt(h.Get(name), 10, 64); err == nil {
			value[key] = n
		}
	}
	return value
}
func (v *Verifier) get(ctx context.Context, route string, target any) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", v.base+route, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "FrontierCloud-release-control")
	if v.token != "" {
		req.Header.Set("Authorization", "Bearer "+v.token)
	}
	response, err := v.client.Do(req)
	if err != nil {
		return nil, &apiFailure{kind: "network"}
	}
	defer response.Body.Close()
	rate := rateInfo(response.Header)
	if response.StatusCode != 200 {
		failure := &apiFailure{kind: "http_status", status: response.StatusCode, rate: rate}
		if response.StatusCode == 429 || response.StatusCode == 403 && response.Header.Get("X-RateLimit-Remaining") == "0" {
			failure.kind = "rate_limited"
			failure.retry = 30
			if n, err := strconv.ParseInt(response.Header.Get("Retry-After"), 10, 64); err == nil {
				failure.retry = max(failure.retry, min(n, 86400))
			}
			if n, err := strconv.ParseInt(response.Header.Get("X-RateLimit-Reset"), 10, 64); err == nil {
				failure.retry = max(failure.retry, min(n-v.now().Unix(), 86400))
			}
		}
		return nil, failure
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, 2*1024*1024+1))
	if err != nil || len(raw) > 2*1024*1024 || !utf8.Valid(raw) {
		return nil, &apiFailure{kind: "unexpected"}
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	if d.Decode(target) != nil {
		return nil, &apiFailure{kind: "unexpected"}
	}
	var tail any
	if d.Decode(&tail) != io.EOF {
		return nil, &apiFailure{kind: "unexpected"}
	}
	return rate, nil
}

type commit struct {
	SHA    string `json:"sha"`
	Commit struct {
		Tree struct {
			SHA string `json:"sha"`
		} `json:"tree"`
	} `json:"commit"`
}
type pull struct {
	Merged   *string `json:"merged_at"`
	MergeSHA string  `json:"merge_commit_sha"`
	Base     struct {
		Ref string `json:"ref"`
	} `json:"base"`
	Head struct {
		Ref  string `json:"ref"`
		SHA  string `json:"sha"`
		Repo struct {
			FullName string `json:"full_name"`
		} `json:"repo"`
	} `json:"head"`
}
type run struct {
	Branch     string  `json:"head_branch"`
	Event      string  `json:"event"`
	SHA        string  `json:"head_sha"`
	Status     string  `json:"status"`
	Conclusion *string `json:"conclusion"`
	Number     int64   `json:"run_number"`
	URL        string  `json:"html_url"`
	Updated    string  `json:"updated_at"`
}

func (v *Verifier) verify(ctx context.Context, now time.Time) (map[string]any, error) {
	return v.verifyTarget(ctx, now, "")
}

// Artifact verifies an exact historical production commit. It never substitutes
// cached HEAD evidence or an older successful CI run. Source validation still
// independently enforces current HEAD for upgrade and ancestry for rollback.
func (v *Verifier) Artifact(parent context.Context, target string) (map[string]any, error) {
	if !ValidSHA(target) {
		return nil, ErrManifest
	}
	select {
	case v.lock <- struct{}{}:
	case <-parent.Done():
		return nil, parent.Err()
	}
	defer func() { <-v.lock }()
	if v.now().Before(v.backoff) {
		return nil, errors.New("release verification rate limited")
	}
	ctx, cancel := context.WithTimeout(parent, 15*time.Second)
	defer cancel()
	value, err := v.verifyTarget(ctx, v.now(), target)
	var failure *apiFailure
	if errors.As(err, &failure) && failure.kind == "rate_limited" {
		v.backoff = v.now().Add(time.Duration(failure.retry) * time.Second)
	}
	if err != nil {
		return nil, err
	}
	return copyValue(value), nil
}

func (v *Verifier) verifyTarget(ctx context.Context, now time.Time, target string) (map[string]any, error) {
	result := map[string]any{"available": false, "branch": v.policy.Branch, "source_branch": v.policy.Source, "status": "unknown", "conclusion": nil, "publishable": false, "checked_at": now.Unix(), "authenticated": v.token != ""}
	var branch struct {
		Commit commit `json:"commit"`
	}
	var rate map[string]any
	var err error
	if target == "" {
		rate, err = v.get(ctx, "/branches/"+url.PathEscape(v.policy.Branch), &branch)
	} else {
		rate, err = v.get(ctx, "/commits/"+target, &branch.Commit)
	}
	if err != nil {
		return nil, err
	}
	if len(rate) != 0 {
		result["rate_limit"] = rate
	}
	sha, tree := branch.Commit.SHA, branch.Commit.Commit.Tree.SHA
	if !ValidSHA(sha) || !ValidSHA(tree) || target != "" && sha != target {
		result["detail"] = "release HEAD tree is unavailable"
		return result, nil
	}
	result["available"], result["sha"], result["tree_sha"] = true, sha, tree
	var pulls []pull
	if _, err = v.get(ctx, "/commits/"+sha+"/pulls?per_page=100", &pulls); err != nil {
		return nil, err
	}
	sources := map[string]bool{}
	for _, p := range pulls {
		if p.Merged != nil && *p.Merged != "" && p.MergeSHA == sha && p.Base.Ref == v.policy.Branch && p.Head.Ref == v.policy.Source && p.Head.Repo.FullName == "wongyiuming/FrontierCloud" && ValidSHA(p.Head.SHA) {
			sources[p.Head.SHA] = true
		}
	}
	if len(sources) != 1 || len(pulls) >= 100 {
		result["detail"] = "release HEAD has no unique merged source PR association"
		return result, nil
	}
	var source string
	for id := range sources {
		source = id
	}
	result["ci_sha"] = source
	var reviewed commit
	if _, err = v.get(ctx, "/commits/"+source, &reviewed); err != nil {
		return nil, err
	}
	if reviewed.SHA != source || reviewed.Commit.Tree.SHA != tree {
		result["detail"] = "release HEAD tree differs from the reviewed PR tree"
		return result, nil
	}
	var runs struct {
		Runs []run `json:"workflow_runs"`
	}
	rate, err = v.get(ctx, "/actions/workflows/docker.yml/runs?event=push&head_sha="+source+"&per_page=20", &runs)
	if err != nil {
		return nil, err
	}
	if len(rate) != 0 {
		result["rate_limit"] = rate
	}
	// The first newest exact source push is authoritative. An older successful
	// rerun cannot override a newer pending/failed result for the same commit.
	sort.SliceStable(runs.Runs, func(i, j int) bool { return runs.Runs[i].Number > runs.Runs[j].Number })
	for _, r := range runs.Runs {
		if r.Branch != v.policy.Source || r.Event != "push" || r.SHA != source {
			continue
		}
		result["status"], result["conclusion"], result["run_number"], result["html_url"], result["updated_at"] = r.Status, r.Conclusion, r.Number, r.URL, r.Updated
		success := r.Status == "completed" && r.Conclusion != nil && *r.Conclusion == "success"
		result["publishable"] = success
		if success {
			result["detail"] = ""
		} else {
			result["detail"] = "reviewed source push CI has not succeeded"
		}
		return result, nil
	}
	result["detail"] = "reviewed source PR head has no matching push CI result"
	return result, nil
}

func (v *Verifier) Status(parent context.Context, force bool) (map[string]any, error) {
	select {
	case v.lock <- struct{}{}:
	case <-parent.Done():
		return nil, parent.Err()
	}
	defer func() { <-v.lock }()
	now := v.now()
	if now.Before(v.backoff) {
		out := copyValue(v.cache)
		out["retry_after_seconds"] = max(0, int64(v.backoff.Sub(now).Seconds()))
		return out, nil
	}
	age := 5 * time.Minute
	if force {
		age = time.Minute
	}
	if v.cache != nil && now.Sub(v.checked) < age {
		return copyValue(v.cache), nil
	}
	ctx, cancel := context.WithTimeout(parent, 15*time.Second)
	defer cancel()
	value, err := v.verify(ctx, now)
	if err != nil {
		if parent.Err() != nil {
			return nil, parent.Err()
		}
		value = map[string]any{"available": false, "branch": v.policy.Branch, "source_branch": v.policy.Source, "status": "unavailable", "conclusion": nil, "publishable": false, "checked_at": now.Unix(), "authenticated": v.token != "", "error_kind": "unexpected", "detail": "GitHub API verification unavailable"}
		var failure *apiFailure
		if errors.As(err, &failure) {
			value["error_kind"] = failure.kind
			if failure.status != 0 {
				value["http_status"] = failure.status
				value["detail"] = fmt.Sprintf("GitHub API HTTP %d", failure.status)
			}
			if len(failure.rate) != 0 {
				value["rate_limit"] = failure.rate
			}
			if failure.kind == "rate_limited" {
				v.backoff = now.Add(time.Duration(failure.retry) * time.Second)
				value["retry_after_seconds"] = failure.retry
			}
		}
		if v.last != nil {
			value["last_verified"] = copyValue(v.last)
		}
	} else if value["available"] == true {
		v.last = copyValue(value)
	}
	v.checked, v.cache = now, copyValue(value)
	return copyValue(value), nil
}
