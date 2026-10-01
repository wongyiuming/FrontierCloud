// Package config loads the Go runtime's deployment contract from the environment.
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
)

const (
	DatabaseSQLite = "sqlite"
	DatabaseMySQL  = "mysql"
)

// Config contains only runtime and store selection. Public product settings
// remain shared with the Python implementation as their handlers are ported.
type Config struct {
	DatabaseType      string
	SQLitePath        string
	MySQLHost         string
	MySQLPort         int
	MySQLDatabase     string
	MySQLUser         string
	MySQLPasswordFile string
	RedisURL          string
	HTTPAddress       string
}

// Load reads configuration from the process environment.
func Load() (Config, error) {
	return LoadFrom(os.Getenv)
}

// LoadFrom makes configuration parsing deterministic in tests.
func LoadFrom(getenv func(string) string) (Config, error) {
	port, err := integer(getenv("MYSQL_PORT"), 3306)
	if err != nil || port < 1 || port > 65535 {
		return Config{}, errors.New("MYSQL_PORT must be an integer from 1 to 65535")
	}
	value := Config{
		DatabaseType:      normalized(getenv("DB_TYPE"), DatabaseSQLite),
		SQLitePath:        fallback(getenv("SQLITE_PATH"), "/data/frontiercloud.db"),
		MySQLHost:         fallback(getenv("MYSQL_HOST"), "mysql"),
		MySQLPort:         port,
		MySQLDatabase:     fallback(getenv("MYSQL_DATABASE"), "office_automation"),
		MySQLUser:         fallback(getenv("MYSQL_USER"), "media_admin"),
		MySQLPasswordFile: fallback(getenv("MYSQL_PASSWORD_FILE"), "/run/frontiercloud-secrets/mysql_password"),
		RedisURL:          fallback(getenv("REDIS_URL"), "redis://redis:6379/0"),
		HTTPAddress:       fallback(getenv("HTTP_ADDR"), ":8000"),
	}
	if value.DatabaseType != DatabaseSQLite && value.DatabaseType != DatabaseMySQL {
		return Config{}, fmt.Errorf("DB_TYPE must be %q or %q", DatabaseSQLite, DatabaseMySQL)
	}
	if value.DatabaseType == DatabaseSQLite && strings.TrimSpace(value.SQLitePath) == "" {
		return Config{}, errors.New("SQLITE_PATH is required when DB_TYPE=sqlite")
	}
	return value, nil
}

func fallback(value, defaultValue string) string {
	if strings.TrimSpace(value) == "" {
		return defaultValue
	}
	return strings.TrimSpace(value)
}

func normalized(value, defaultValue string) string {
	return strings.ToLower(fallback(value, defaultValue))
}

func integer(value string, defaultValue int) (int, error) {
	if strings.TrimSpace(value) == "" {
		return defaultValue, nil
	}
	return strconv.Atoi(strings.TrimSpace(value))
}
