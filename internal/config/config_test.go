package config

import "testing"

func environment(values map[string]string) func(string) string {
	return func(name string) string { return values[name] }
}

func TestDefaultIsSQLite(t *testing.T) {
	value, err := LoadFrom(environment(nil))
	if err != nil {
		t.Fatal(err)
	}
	if value.DatabaseType != DatabaseSQLite || value.SQLitePath != "/data/frontiercloud.db" {
		t.Fatalf("unexpected defaults: %+v", value)
	}
}

func TestMySQLSelection(t *testing.T) {
	value, err := LoadFrom(environment(map[string]string{
		"DB_TYPE":        "MYSQL",
		"MYSQL_HOST":     "database.example",
		"MYSQL_PORT":     "3307",
		"MYSQL_DATABASE": "frontiercloud",
		"MYSQL_USER":     "frontiercloud",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if value.DatabaseType != DatabaseMySQL || value.MySQLHost != "database.example" || value.MySQLPort != 3307 {
		t.Fatalf("unexpected MySQL config: %+v", value)
	}
}

func TestRejectsUnknownDatabase(t *testing.T) {
	if _, err := LoadFrom(environment(map[string]string{"DB_TYPE": "postgres"})); err == nil {
		t.Fatal("unknown DB_TYPE was accepted")
	}
}
