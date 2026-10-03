package deployment

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

type composeService struct {
	Image       string            `json:"image"`
	Command     []string          `json:"command"`
	User        string            `json:"user"`
	Environment map[string]string `json:"environment"`
	Build       struct {
		Dockerfile string            `json:"dockerfile"`
		Args       map[string]string `json:"args"`
	} `json:"build"`
	DependsOn map[string]struct {
		Condition string `json:"condition"`
	} `json:"depends_on"`
	Volumes []struct {
		Target   string `json:"target"`
		ReadOnly bool   `json:"read_only"`
	} `json:"volumes"`
	Healthcheck struct {
		Test []string `json:"test"`
	} `json:"healthcheck"`
}

// Opt-in output from the real Docker Compose parser, never a replacement YAML
// parser that merely assumes how anchors/interpolation/overrides will merge.
func TestActualComposeSQLiteAndMySQLSelection(t *testing.T) {
	dir := os.Getenv("FRONTIERCLOUD_TEST_COMPOSE_JSON_DIR")
	if dir == "" {
		t.Skip("actual Compose configurations not selected")
	}
	for _, kind := range []string{"sqlite", "mysql"} {
		t.Run(kind, func(t *testing.T) {
			data, err := os.ReadFile(filepath.Join(dir, kind+".json"))
			if err != nil || len(data) > 4*1024*1024 {
				t.Fatal("bounded real Compose output required", err)
			}
			var spec struct {
				Name     string                    `json:"name"`
				Services map[string]composeService `json:"services"`
			}
			if err := json.Unmarshal(data, &spec); err != nil {
				t.Fatal(err)
			}
			if spec.Name == "" {
				t.Fatal("project identity missing")
			}
			for _, name := range []string{"web", "secrets-init", "media-init", "updater"} {
				s, ok := spec.Services[name]
				if !ok || strings.Contains(strings.ToLower(s.Image), "python") || len(s.Command) != 1 {
					t.Fatal("native service contract", name, s)
				}
				want := map[string]string{"web": "serve", "updater": "serve", "secrets-init": "init-secrets", "media-init": "init-media"}[name]
				if s.Command[0] != want {
					t.Fatal("service delegates to a script", name)
				}
				if s.Build.Args["REVISION"] == "" || !strings.HasSuffix(s.Image, ":"+s.Build.Args["REVISION"]) {
					t.Fatal("immutable image revision missing", name)
				}
			}
			web, updater := spec.Services["web"], spec.Services["updater"]
			if web.Build.Dockerfile != "Dockerfile.gin" || updater.Build.Dockerfile != "updater/Dockerfile.gin" || web.User != "10001:10001" || updater.User != "0:0" {
				t.Fatal("runtime/privilege boundary changed")
			}
			if web.Environment["DB_TYPE"] != kind || web.Environment["SQLITE_PATH"] != "/app/data/frontiercloud.db" || web.Environment["DATA_ROOT"] != "/app/data" {
				t.Fatal("selected store/data volume mismatch")
			}
			if !reflect.DeepEqual(web.Healthcheck.Test, []string{"CMD", "/app/frontiercloud", "healthcheck"}) {
				t.Fatal("health probe delegates outside native binary")
			}
			if updater.Environment["UPDATER_PROJECT"] != spec.Name || updater.Environment["UPDATER_DATA_DIRECTORY"] != "/data" {
				t.Fatal("updater can target another project or data volume")
			}
			for _, pair := range []struct {
				s  composeService
				ro bool
			}{{web, true}, {updater, false}} {
				found := false
				for _, v := range pair.s.Volumes {
					if v.Target == "/run/frontiercloud-updater" {
						found = v.ReadOnly == pair.ro
					}
					if v.Target == "/var/run/docker.sock" && pair.ro {
						t.Fatal("Web gained Docker authority")
					}
				}
				if !found {
					t.Fatal("control mount permission boundary changed")
				}
			}
			_, mysql := spec.Services["mysql"]
			dependency, depends := web.DependsOn["mysql"]
			if kind == "sqlite" && (mysql || depends) {
				t.Fatal("SQLite launches/requires MySQL")
			}
			if kind == "mysql" && (!mysql || !depends || dependency.Condition != "service_healthy" || web.Environment["MYSQL_HOST"] != "mysql") {
				t.Fatal("MySQL overlay omitted required healthy store")
			}
		})
	}
}
