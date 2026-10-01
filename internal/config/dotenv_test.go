package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDotEnvParsingAndEnvironmentPrecedence(t *testing.T) {
	t.Chdir(t.TempDir())
	payload := "\ufeff# ignored\nexport DB_TYPE=mysql\nMYSQL_HOST='database.local' # suffix\nSERVER_NAME=example.com # public\nREDIS_URL=\"redis://localhost:6379/2\"\nLOG_LEVEL=INFO\n"
	if err := os.WriteFile(".env", []byte(payload), 0600); err != nil {
		t.Fatal(err)
	}
	file, err := readDotEnv(".env")
	if err != nil || file["MYSQL_HOST"] != "database.local" || file["SERVER_NAME"] != "example.com" {
		t.Fatalf("parse: %v %v", file, err)
	}
	t.Setenv("DB_TYPE", "sqlite")
	cfg, err := Load()
	if err != nil || cfg.DatabaseType != "sqlite" {
		t.Fatalf("environment did not override: %v", err)
	}
	t.Setenv("MYSQL_HOST", "")
	cfg, err = Load()
	if err != nil || cfg.MySQLHost != "mysql" {
		t.Fatal("explicit empty env did not override .env")
	}
}

func TestDotEnvRejectsMalformedWithoutLeakingContents(t *testing.T) {
	for _, payload := range []string{"KEY='unterminated", "not-valid=value", "KEY=\"quoted\" trailing", "NO_ASSIGNMENT"} {
		file := filepath.Join(t.TempDir(), ".env")
		if err := os.WriteFile(file, []byte(payload), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := readDotEnv(file); err == nil {
			t.Fatalf("malformed input accepted")
		}
	}
}
