package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/backup"
	"github.com/wongyiuming/FrontierCloud/internal/config"
)

// No initialization, role changes, restore or background workers. Successful
// output explicitly remains restore_ready=false until the other gates exist.
func verifyBackupCommand(arguments []string, output io.Writer) error {
	flags := flag.NewFlagSet("verify-backup", flag.ContinueOnError)
	file := flags.String("file", "", "local v2 JSONL artifact (mutually exclusive with master-id)")
	master := flags.String("master-id", "", "historical Master ID in the configured cold backup store")
	generation := flags.Int64("generation", 0, "exact positive backup generation")
	checksum := flags.String("sha256", "", "expected SHA-256, required for a local file")
	scratch := flags.String("scratch-dir", "", "private scratch directory, never an authoritative database directory")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	if flags.NArg() != 0 || *generation <= 0 || *scratch == "" || (*file == "") == (*master == "") || *file != "" && *checksum == "" || *master != "" && *checksum != "" {
		return errors.New("verify-backup requires generation, scratch-dir and either file + sha256 or master-id")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	var report backup.Report
	if *file != "" {
		info, err := os.Lstat(*file)
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return errors.New("backup input must be a regular file")
		}
		source, err := os.Open(*file)
		if err != nil {
			return err
		}
		defer source.Close()
		opened, err := source.Stat()
		if err != nil {
			return err
		}
		if !os.SameFile(info, opened) {
			return errors.New("backup input changed while opening")
		}
		report, err = backup.Preflight(ctx, source, backup.Expectation{Generation: *generation, Checksum: *checksum, Bytes: opened.Size()}, *scratch)
		if err != nil {
			return err
		}
	} else {
		settings, err := config.Load()
		if err != nil {
			return err
		}
		database, err := openExistingStore(ctx, settings)
		if err != nil {
			return err
		}
		defer database.Close()
		report, err = backup.InspectReady(ctx, database.Backups(), *master, *generation, *scratch)
		if err != nil {
			return err
		}
	}
	return json.NewEncoder(output).Encode(report)
}
