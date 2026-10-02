package updater

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

func TestCleanupOnlyExactOwnObsoleteTagsNeverForceOrPrune(t *testing.T) {
	current, previous, stale := testCurrent, testTarget, strings.Repeat("3", 40)
	tags := []string{"frontiercloud-web:" + current, "frontiercloud-nginx:" + previous, "frontiercloud-web:" + stale, "frontiercloud-updater:" + stale, "frontiercloud-nginx:" + stale, "foreign:" + stale}
	var removed []string
	e := engineFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1.52/images/json" {
			json.NewEncoder(w).Encode([]Image{{RepoTags: tags}})
			return
		}
		if r.Method == "DELETE" {
			removed = append(removed, strings.TrimPrefix(r.URL.Path, "/v1.52/images/"))
			if r.URL.Query().Get("force") != "false" || r.URL.Query().Get("noprune") != "true" {
				t.Error("unsafe cleanup options")
			}
			if strings.Contains(r.URL.Path, "updater") {
				w.WriteHeader(409)
				return
			}
			w.WriteHeader(204)
			return
		}
		ref := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/v1.52/images/"), "/json")
		match := releaseTag.FindStringSubmatch(ref)
		if match == nil {
			t.Fatal("inspected unrelated tag")
		}
		i := Image{ID: "sha256:" + strings.Repeat("a", 64)}
		i.Config.Labels = map[string]string{"frontiercloud.revision": match[2], "frontiercloud.component": match[1], "frontiercloud.runtime": "go", "frontiercloud.schema-generation": "2", "frontiercloud.project": "native-test"}
		if match[1] == "nginx" {
			i.Config.Labels["frontiercloud.project"] = "other-stack"
		}
		json.NewEncoder(w).Encode(i)
	})
	e.Project = "native-test"
	if err := e.Cleanup(context.Background(), current, previous); err != nil {
		t.Fatal(err)
	}
	if len(removed) != 2 || removed[0] != "frontiercloud-web:"+stale || removed[1] != "frontiercloud-updater:"+stale {
		t.Fatal("cleanup crossed retention/project scope", removed)
	}
}
