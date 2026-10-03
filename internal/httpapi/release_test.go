package httpapi

import (
	"context"
	"sync"

	"github.com/wongyiuming/FrontierCloud/internal/release"
)

type testReleaseAgent struct {
	mu           sync.Mutex
	starts       int
	status       map[string]any
	busy         bool
	capabilities []string
	lastManifest any
}

func (a *testReleaseAgent) Request(_ context.Context, value map[string]any) (map[string]any, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if value["action"] == "status" {
		out := map[string]any{"ok": true, "status": a.status}
		if a.capabilities != nil {
			out["capabilities"] = a.capabilities
		}
		return out, nil
	}
	if whole, ok := value["release_manifest"]; ok {
		if _, selected := value["target_sha"]; selected {
			panic("Master selected Follower artifact")
		}
		manifest, err := release.ManifestFromValue(whole)
		if err != nil {
			return map[string]any{"ok": false}, nil
		}
		a.lastManifest = whole
		if a.busy {
			return map[string]any{"ok": false, "status": map[string]any{"target_manifest": whole, "mode": value["mode"], "state": "running"}}, nil
		}
		a.starts++
		id, _ := manifest.ID()
		return map[string]any{"ok": true, "accepted": true, "release_id": id}, nil
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
