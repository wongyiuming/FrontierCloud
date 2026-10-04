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

func TestProjectImageTagsStayBoundedAndCleanupCannotCrossNamespaces(t *testing.T) {
	const project = "native-test"
	if got := releaseImageTag(project, testTarget, "web"); got != "frontiercloud-web:"+testTarget+"-825a03693b16" {
		t.Fatal("cross-runtime project tag vector changed", got)
	}
	if len(strings.Split(releaseImageTag(strings.Repeat("x", 128), testTarget, "web"), ":")[1]) > 128 {
		t.Fatal("Docker tag limit exceeded")
	}
	stale := strings.Repeat("3", 40)
	own := releaseImageTag(project, stale, "web")
	foreign := releaseImageTag("foreign", stale, "web")
	var removed []string
	e := engineFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1.52/images/json" {
			_ = json.NewEncoder(w).Encode([]Image{{RepoTags: []string{own, foreign, releaseImageTag(project, testCurrent, "web")}}})
			return
		}
		ref := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/v1.52/images/"), "/json")
		if ref != own {
			t.Error("foreign namespace inspected or mutated", ref)
		}
		if r.Method == "DELETE" {
			if r.URL.Query().Get("force") != "false" || r.URL.Query().Get("noprune") != "true" {
				t.Error("unsafe scoped retirement")
			}
			removed = append(removed, ref)
			w.WriteHeader(204)
			return
		}
		image := Image{ID: "sha256:" + strings.Repeat("a", 64)}
		// Even a forged matching owner label on the foreign namespace must not
		// authorize its lookup/removal. The tag namespace is checked first.
		image.Config.Labels = map[string]string{"frontiercloud.revision": stale, "frontiercloud.component": "web", "frontiercloud.runtime": "go", "frontiercloud.schema-generation": "2", "frontiercloud.project": project}
		_ = json.NewEncoder(w).Encode(image)
	})
	e.Project = project
	if err := e.Cleanup(context.Background(), testCurrent, testTarget); err != nil || len(removed) != 1 || removed[0] != own {
		t.Fatal("scoped image cleanup changed ownership", err, removed)
	}
}

func TestNativeFixtureRetirementRequiresNamespaceAndFullImmutableOwner(t *testing.T) {
	project := "fc-native-stack-" + strings.Repeat("a", 24)
	own := releaseImageTag(project, testTarget, "web")
	wrongOwner := releaseImageTag(project, testTarget, "nginx")
	missing := releaseImageTag(project, testTarget, "updater")
	foreign := releaseImageTag("other-project", testTarget, "web")
	var removed []string
	e := engineFixture(t, func(w http.ResponseWriter, r *http.Request) {
		ref := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/v1.52/images/"), "/json")
		if ref == foreign || ref == "frontiercloud-web:"+testTarget {
			t.Error("foreign/legacy namespace inspected", ref)
		}
		if r.Method == "DELETE" {
			if ref != own || r.URL.Query().Get("force") != "false" || r.URL.Query().Get("noprune") != "true" {
				t.Error("fixture retirement crossed proven scope", ref)
			}
			removed = append(removed, ref)
			w.WriteHeader(204)
			return
		}
		if ref == missing {
			w.WriteHeader(404)
			return
		}
		match := releaseTag.FindStringSubmatch(ref)
		image := Image{ID: "sha256:" + strings.Repeat("b", 64)}
		image.Config.Labels = map[string]string{"frontiercloud.revision": match[2], "frontiercloud.component": match[1], "frontiercloud.runtime": "go", "frontiercloud.schema-generation": "2", "frontiercloud.project": project}
		if ref == wrongOwner {
			image.Config.Labels["frontiercloud.project"] = "other-project"
		}
		_ = json.NewEncoder(w).Encode(image)
	})
	e.Project = project
	retireNativeFixtureImages(context.Background(), e, []string{own, wrongOwner, missing, foreign, "frontiercloud-web:" + testTarget})
	if len(removed) != 1 || removed[0] != own {
		t.Fatal("fixture retirement lacked exact owner proof", removed)
	}
}
