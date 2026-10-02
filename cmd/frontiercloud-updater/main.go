package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/release"
	"github.com/wongyiuming/FrontierCloud/internal/updater"
)

// Revision is compiled into the immutable updater image, never inferred from
// a mutable mounted checkout or trusted from an environment variable.
var Revision string

func main() {
	if err := command(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "native updater stopped:", err)
		os.Exit(1)
	}
}
func setting(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
func command(arguments []string) error {
	action := "serve"
	if len(arguments) > 0 {
		action = arguments[0]
	}
	if len(arguments) > 1 || (action != "serve" && action != "handoff") {
		return errors.New("updater requires serve or handoff")
	}
	if !release.ValidSHA(Revision) {
		return errors.New("compiled release revision required")
	}
	workspace := setting("UPDATER_WORKSPACE", "/workspace")
	branch := setting("RELEASE_BRANCH", "main")
	if branch != "main" && branch != "gin_main" {
		return errors.New("unsupported updater production branch")
	}
	x := &updater.DockerExecutor{Source: updater.Source{Directory: workspace, Branch: branch}, Socket: setting("UPDATER_DOCKER_SOCKET", "/var/run/docker.sock"), Project: os.Getenv("UPDATER_PROJECT"), ControlDirectory: setting("UPDATER_CONTROL_DIRECTORY", "/run/frontiercloud-updater"), Runtime: Revision, DataDirectory: setting("UPDATER_DATA_DIRECTORY", "/workspace/data")}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if action == "handoff" {
		bounded, cancel := context.WithTimeout(ctx, 10*time.Minute)
		defer cancel()
		return x.Handoff(bounded)
	}
	recovery, cancel := context.WithTimeout(ctx, 10*time.Minute)
	d, err := updater.NewDaemon(recovery, x.ControlDirectory, setting("UPDATER_MAINTENANCE_DIRECTORY", "/run/frontiercloud-maintenance"), Revision, branch, x)
	cancel()
	if err != nil {
		return errors.New("native updater startup proof failed")
	}
	defer d.Close()
	return d.Serve(ctx)
}
