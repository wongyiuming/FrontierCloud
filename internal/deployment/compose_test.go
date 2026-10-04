package deployment

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestNativeCompilationBudgetDoesNotChangeRuntimeEnvironment(t *testing.T) {
	root, err := os.ReadFile(filepath.Join("..", "..", "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	alias, err := os.ReadFile(filepath.Join("..", "..", "Dockerfile.gin"))
	if err != nil || strings.ReplaceAll(string(root), "\r\n", "\n") != strings.ReplaceAll(string(alias), "\r\n", "\n") {
		t.Fatal("native root and explicit alias diverged", err)
	}
	for _, file := range []string{"Dockerfile", filepath.Join("updater", "Dockerfile.gin")} {
		contents, err := os.ReadFile(filepath.Join("..", "..", file))
		if err != nil {
			t.Fatal(err)
		}
		text := string(contents)
		if !strings.Contains(text, "GOMAXPROCS=2 CGO_ENABLED=0 /usr/local/go/bin/go build -p=1") || strings.Contains(text, "ENV GOMAXPROCS") {
			t.Fatal("compile budget missing or incorrectly applied to runtime", file)
		}
		if file == "Dockerfile" && !strings.Contains(text, "GOMAXPROCS=2 CGO_ENABLED=0 /usr/local/go/bin/go test -p=1 ./...") {
			t.Fatal("native image unit compilation is unbounded")
		}
	}
}

type composeService struct {
	Image       string            `json:"image"`
	Command     []string          `json:"command"`
	User        string            `json:"user"`
	CapAdd      []string          `json:"cap_add"`
	CapDrop     []string          `json:"cap_drop"`
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

func TestActualReferenceSQLiteAndMySQLSelection(t *testing.T) {
	dir := os.Getenv("FRONTIERCLOUD_TEST_COMPOSE_JSON_DIR")
	if dir == "" {
		t.Skip("actual reference Compose configurations not selected")
	}
	for _, kind := range []string{"sqlite", "mysql"} {
		t.Run(kind, func(t *testing.T) {
			data, err := os.ReadFile(filepath.Join(dir, "python-"+kind+".json"))
			if err != nil || len(data) > 4*1024*1024 {
				t.Fatal("bounded actual reference Compose required", err)
			}
			var spec struct {
				Name     string                    `json:"name"`
				Services map[string]composeService `json:"services"`
			}
			if err := json.Unmarshal(data, &spec); err != nil {
				t.Fatal(err)
			}
			web, ok := spec.Services["web"]
			if !ok || spec.Name == "" || web.Build.Dockerfile != "Dockerfile.python" || web.Environment["DB_TYPE"] != kind || web.Environment["RELEASE_BRANCH"] != "main" || web.Environment["RELEASE_SOURCE_BRANCH"] != "dev" || !reflect.DeepEqual(web.Healthcheck.Test, []string{"CMD", "python", "-m", "app.services.health_probe"}) {
				t.Fatal("explicit reference runtime/profile/store changed")
			}
			_, mysql := spec.Services["mysql"]
			updater := spec.Services["updater"]
			if updater.Environment["UPDATER_PROJECT"] != spec.Name || updater.Environment["UPDATER_DATA_DIRECTORY"] != "/data" {
				t.Fatal("reference updater project/data selector changed")
			}
			dependency, depends := web.DependsOn["mysql"]
			if kind == "sqlite" && (mysql || depends) {
				t.Fatal("reference SQLite requires MySQL")
			}
			if kind == "mysql" && (!mysql || !depends || dependency.Condition != "service_healthy") {
				t.Fatal("reference MySQL omitted store readiness")
			}
			for _, service := range spec.Services {
				for _, mount := range service.Volumes {
					if mount.Target == "/var/run/docker.sock" && service.Build.Dockerfile == "Dockerfile.python" {
						t.Fatal("reference Web gained Docker socket")
					}
				}
			}
		})
	}
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
			if len(web.CapAdd) != 0 || !reflect.DeepEqual(web.CapDrop, []string{"ALL"}) || !reflect.DeepEqual(updater.CapDrop, []string{"ALL"}) || !reflect.DeepEqual(updater.CapAdd, []string{"CHOWN", "DAC_OVERRIDE"}) {
				t.Fatal("native Web/agent capability boundary changed")
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
