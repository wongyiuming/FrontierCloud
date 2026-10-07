package updater

import (
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/release"
)

// Source never checks out/reset a commit. Images are built from immutable git
// archives, excluding working-tree secrets, data, untracked files and .git.
type Source struct{ Directory, Branch string }
type boundedOutput struct {
	raw     []byte
	maximum int
}

func (b *boundedOutput) Write(value []byte) (int, error) {
	if len(value) > b.maximum-len(b.raw) {
		return 0, ErrState
	}
	b.raw = append(b.raw, value...)
	return len(value), nil
}
func (s Source) git(parent context.Context, timeout time.Duration, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.Dir = s.Directory
	output := &boundedOutput{maximum: 1 << 20}
	cmd.Stdout = output
	cmd.Stderr = io.Discard
	if err := cmd.Run(); err != nil {
		return "", errors.New("release source validation failed")
	}
	return strings.TrimSpace(string(output.raw)), nil
}
func (s Source) Validate(ctx context.Context, target, mode string) error {
	if !release.ValidSHA(target) || (s.Branch != "main" && s.Branch != "gin_main") || (mode != "upgrade" && mode != "rollback") || s.Directory == "" {
		return ErrState
	}
	status, err := s.git(ctx, 20*time.Second, "status", "--porcelain", "--untracked-files=no")
	if err != nil {
		return err
	}
	if status != "" {
		return errors.New("tracked source changes block release")
	}
	ref := "refs/remotes/origin/" + s.Branch
	if _, err = s.git(ctx, 120*time.Second, "fetch", "--no-tags", "origin", "+refs/heads/"+s.Branch+":"+ref); err != nil {
		return err
	}
	head, err := s.git(ctx, 20*time.Second, "rev-parse", "--verify", ref+"^{commit}")
	if err != nil || !release.ValidSHA(head) {
		return ErrState
	}
	if mode == "upgrade" && target != head {
		return errors.New("release target is not current production HEAD")
	}
	if _, err = s.git(ctx, 20*time.Second, "cat-file", "-e", target+"^{commit}"); err != nil {
		return err
	}
	_, err = s.git(ctx, 20*time.Second, "merge-base", "--is-ancestor", target, ref)
	return err
}

var archivePaths = []string{"Dockerfile", "Dockerfile.gin", "go.mod", "go.sum", "cmd", "internal", "migrations", "protocol", "static", "nginx", "updater/Dockerfile", "updater/Dockerfile.gin"}

func (s Source) Archive(ctx context.Context, target string) (io.ReadCloser, error) {
	if !release.ValidSHA(target) {
		return nil, ErrState
	}
	args := append([]string{"archive", "--format=tar", target, "--"}, archivePaths...)
	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.Dir = s.Directory
	cmd.Stderr = io.Discard
	pipe, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err = cmd.Start(); err != nil {
		pipe.Close()
		return nil, err
	}
	return &archiveStream{ReadCloser: pipe, cmd: cmd}, nil
}

type archiveStream struct {
	io.ReadCloser
	cmd     *exec.Cmd
	done    bool
	mu      sync.Mutex
	closed  bool
	waitErr error
}

func (a *archiveStream) Read(value []byte) (int, error) {
	n, err := a.ReadCloser.Read(value)
	if err == io.EOF {
		a.mu.Lock()
		defer a.mu.Unlock()
		if !a.done {
			a.done = true
			a.waitErr = a.cmd.Wait()
		}
		if a.waitErr != nil {
			return n, errors.New("release archive failed")
		}
	}
	return n, err
}
func (a *archiveStream) Close() error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closed {
		return a.waitErr
	}
	a.closed = true
	err := a.ReadCloser.Close()
	if errors.Is(err, os.ErrClosed) {
		err = nil
	} // Cmd.Wait closes StdoutPipe.
	if !a.done {
		a.done = true
		a.cmd.Process.Kill()
		if e := a.cmd.Wait(); e != nil {
			a.waitErr = errors.New("release archive interrupted")
			return errors.Join(err, a.waitErr)
		}
	}
	return err
}
