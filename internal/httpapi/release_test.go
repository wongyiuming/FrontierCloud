package httpapi

import (
	"context"
	"sync"
)

type testReleaseAgent struct {
	mu     sync.Mutex
	starts int
	status map[string]any
	busy   bool
}

func (a *testReleaseAgent) Request(_ context.Context, value map[string]any) (map[string]any, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if value["action"] == "status" {
		return map[string]any{"ok": true, "status": a.status}, nil
	}
	if a.busy {
		return map[string]any{"ok": false, "status": map[string]any{"target_sha": value["target_sha"], "mode": value["mode"], "state": "running"}}, nil
	}
	a.starts++
	return map[string]any{"ok": true, "accepted": true, "target_sha": value["target_sha"], "mode": value["mode"]}, nil
}

type testReleaseEvidence struct {
	sha         string
	publishable bool
}

func (e *testReleaseEvidence) Status(context.Context, bool) (map[string]any, error) {
	return map[string]any{"sha": e.sha, "publishable": e.publishable}, nil
}
