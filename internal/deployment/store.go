// Package deployment prevents a configuration edit from silently replacing a
// node's authoritative database. Selection is not a data migration.
package deployment

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
)

const Binding = ".native-store"
const lockName = ".native-store.lock"

var ErrSelection = errors.New("authoritative store selection changed or is unsafe; DB_TYPE is not a migration")

func fingerprint(c config.Config) (string, error) {
	var parts []string
	switch c.DatabaseType {
	case config.DatabaseSQLite:
		p, err := filepath.Abs(c.SQLitePath)
		if err != nil || strings.ContainsAny(p, "\r\n\x00") {
			return "", ErrSelection
		}
		parts = []string{"sqlite", filepath.Clean(p)}
	case config.DatabaseMySQL:
		// User/password rotation does not change the selected database.
		parts = []string{"mysql", strings.ToLower(c.MySQLHost), strconv.Itoa(c.MySQLPort), c.MySQLDatabase}
	default:
		return "", ErrSelection
	}
	for _, p := range parts {
		if p == "" || strings.ContainsAny(p, "\r\n\x00") {
			return "", ErrSelection
		}
	}
	h := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return "native-store-v1:" + hex.EncodeToString(h[:]) + "\n", nil
}

// Bind runs before opening/initializing a writer. An existing vault without a
// binding must prove its identity in the selected existing store, read-only.
// The binding is durable before first initialization so a crash between vault
// publication and SQL identity commit can retry the SAME store, not another.
func Bind(ctx context.Context, c config.Config, proveExisting func(context.Context) error) error {
	want, err := fingerprint(c)
	if err != nil {
		return err
	}
	info, err := os.Lstat(c.DataRoot)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return ErrSelection
	}
	r, err := os.OpenRoot(c.DataRoot)
	if err != nil {
		return err
	}
	defer r.Close()
	opened, err := r.Stat(".")
	if err != nil || !os.SameFile(info, opened) {
		return ErrSelection
	}
	info, err = r.Lstat(lockName)
	if err == nil && (!info.Mode().IsRegular() || info.Size() != 0) {
		return ErrSelection
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	f, err := r.OpenFile(lockName, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	opened, err = f.Stat()
	info, found := r.Lstat(lockName)
	if err != nil || found != nil || !info.Mode().IsRegular() || info.Size() != 0 || !os.SameFile(info, opened) {
		f.Close()
		return ErrSelection
	}
	if err = fsutil.InheritOwner(f, r); err != nil {
		f.Close()
		return err
	}
	done, err := filelease.Acquire(ctx, f, true)
	if err != nil {
		return err
	}
	defer done()
	info, err = r.Lstat(Binding)
	if err == nil {
		if !info.Mode().IsRegular() || info.Size() != int64(len(want)) {
			return ErrSelection
		}
		f, err := r.Open(Binding)
		if err != nil {
			return err
		}
		defer f.Close()
		opened, err := f.Stat()
		if err != nil || !os.SameFile(info, opened) {
			return ErrSelection
		}
		raw, err := io.ReadAll(io.LimitReader(f, int64(len(want))+1))
		if err != nil || string(raw) != want {
			return ErrSelection
		}
		// Retry a prior directory-sync failure before admitting a writer.
		return fsutil.SyncDirectory(r, ".")
	}
	if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if _, err = os.Lstat(filepath.Join(c.SecretsDirectory, "node-vault.key")); err == nil {
		if proveExisting == nil || proveExisting(ctx) != nil {
			return ErrSelection
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return ErrSelection
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	var nonce [16]byte
	if _, err = rand.Read(nonce[:]); err != nil {
		return err
	}
	name := ".native-store-write-" + hex.EncodeToString(nonce[:])
	f, err = r.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer r.Remove(name)
	if err = fsutil.InheritOwner(f, r); err == nil {
		_, err = f.WriteString(want)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil || closeErr != nil {
		return errors.Join(err, closeErr)
	}
	if err = r.Rename(name, Binding); err != nil {
		return err
	}
	return fsutil.SyncDirectory(r, ".")
}
