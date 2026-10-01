// Package bootstrap implements initialization tasks without a Python sidecar.
package bootstrap

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const applicationID = 10001

var secretNames = []string{"mysql_password", "mysql_root_password", "admin_key", "metrics_token"}

// InitializeSecrets preserves the Python initializer's durable volume contract.
func InitializeSecrets(root string) error {
	if err := os.MkdirAll(root, 0o700); err != nil {
		return fmt.Errorf("create secret directory: %w", err)
	}
	if err := os.Chmod(root, 0o700); err != nil {
		return fmt.Errorf("protect secret directory: %w", err)
	}
	if err := os.Chown(root, applicationID, applicationID); err != nil {
		return fmt.Errorf("own secret directory: %w", err)
	}
	initializing := filepath.Join(root, ".initializing")
	initialized := filepath.Join(root, ".initialized")
	announcement := filepath.Join(root, ".announce-once")
	paths := map[string]string{}
	for _, name := range secretNames {
		paths[name] = filepath.Join(root, name)
	}

	complete := true
	for _, path := range paths {
		complete = complete && validSecret(path)
	}
	if complete && !exists(initializing) && !exists(initialized) {
		if err := atomicWrite(initialized, []byte("1\n"), 0o600); err != nil {
			return err
		}
		return ownAll(paths)
	}

	legacyUpgrade := !exists(initialized) && !exists(initializing) &&
		validSecret(paths["mysql_password"]) && validSecret(paths["mysql_root_password"]) &&
		validSecret(paths["admin_key"]) && !validSecret(paths["metrics_token"])
	firstInitialization := !exists(initialized) && !legacyUpgrade
	if firstInitialization && !exists(initializing) {
		if err := atomicWrite(initializing, []byte("1\n"), 0o600); err != nil {
			return err
		}
	}

	created := map[string]bool{}
	for name, path := range paths {
		if validSecret(path) {
			if err := os.Chown(path, applicationID, applicationID); err != nil {
				return err
			}
			continue
		}
		secret := make([]byte, 48)
		if _, err := rand.Read(secret); err != nil {
			return fmt.Errorf("generate %s: %w", name, err)
		}
		value := base64.RawURLEncoding.EncodeToString(secret) + "\n"
		if err := atomicWrite(path, []byte(value), 0o600); err != nil {
			return err
		}
		created[name] = true
	}

	pending := pendingNames(announcement)
	if firstInitialization {
		for _, name := range secretNames {
			pending[name] = true
		}
	} else {
		for name := range created {
			pending[name] = true
		}
	}
	if len(pending) > 0 {
		names := make([]string, 0, len(pending))
		for name := range pending {
			names = append(names, name)
		}
		sort.Strings(names)
		encoded, err := json.Marshal(names)
		if err != nil {
			return err
		}
		if err := atomicWrite(announcement, append(encoded, '\n'), 0o600); err != nil {
			return err
		}
	}
	if !exists(initialized) {
		if err := atomicWrite(initialized, []byte("1\n"), 0o600); err != nil {
			return err
		}
	}
	if err := os.Remove(initializing); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return syncDirectory(root)
}

func atomicWrite(path string, value []byte, mode os.FileMode) error {
	directory := filepath.Dir(path)
	temporary, err := os.CreateTemp(directory, "."+filepath.Base(path)+".*.new")
	if err != nil {
		return fmt.Errorf("create temporary secret: %w", err)
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	if err := temporary.Chmod(mode); err != nil {
		temporary.Close()
		return err
	}
	if _, err := temporary.Write(value); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Chown(temporaryName, applicationID, applicationID); err != nil {
		return err
	}
	if err := os.Rename(temporaryName, path); err != nil {
		return err
	}
	if err := os.Chown(path, applicationID, applicationID); err != nil {
		return err
	}
	return syncDirectory(directory)
}

func validSecret(path string) bool {
	value, err := os.ReadFile(path)
	return err == nil && strings.TrimSpace(string(value)) != ""
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func ownAll(paths map[string]string) error {
	for _, path := range paths {
		if err := os.Chown(path, applicationID, applicationID); err != nil {
			return err
		}
	}
	return nil
}

func pendingNames(path string) map[string]bool {
	result := map[string]bool{}
	value, err := os.ReadFile(path)
	if err != nil {
		return result
	}
	var names []string
	if json.Unmarshal(value, &names) != nil {
		for _, name := range secretNames {
			result[name] = true
		}
		return result
	}
	known := map[string]bool{}
	for _, name := range secretNames {
		known[name] = true
	}
	for _, name := range names {
		if !known[name] {
			for _, fallback := range secretNames {
				result[fallback] = true
			}
			return result
		}
		result[name] = true
	}
	return result
}

func syncDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer directory.Close()
	_ = directory.Sync()
	return nil
}
