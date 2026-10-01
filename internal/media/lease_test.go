package media

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestOtherWorkerBlocksAmbiguousRenameAndCanRecover(t *testing.T) {
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	other, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	svc.repository = &lostRename{MediaRepository: db.Media()}
	if _, err := svc.Rename(ctx, "music/artist", "moved", store.AdminAudit{}); !errors.Is(err, ErrRecovery) {
		t.Fatal(err)
	}
	if err := other.Ready(ctx); !errors.Is(err, ErrRecovery) {
		t.Fatalf("other worker served uncertain state: %v", err)
	}
	// Recovery evidence must also block peers if a worker was killed before
	// setting the explicit flag, as simulated by removing that flag only.
	if err := os.Remove(filepath.Join(root, ".recovery-required")); err != nil {
		t.Fatal(err)
	}
	if err := other.Ready(ctx); !errors.Is(err, ErrRecovery) {
		t.Fatalf("durable intent ignored without flag: %v", err)
	}
	recovered, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	recovered.Close()
	if err := other.Ready(ctx); err != nil {
		t.Fatalf("peer did not resume after complete recovery: %v", err)
	}
}
func TestVolumeLeaseWaitHonorsCancellation(t *testing.T) {
	root, db, svc := deleteFixture(t)
	other, err := New(root, db.Media(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	release, err := svc.acquire(context.Background(), true)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if err := other.Ready(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("volume lease cancellation: %v", err)
	}
}

type pausedStageReader struct {
	once             sync.Once
	started, proceed chan struct{}
	reader           io.Reader
}

func (r *pausedStageReader) Read(p []byte) (int, error) {
	r.once.Do(func() { close(r.started); <-r.proceed })
	return r.reader.Read(p)
}
func TestWorkerStartupDoesNotRemoveActiveMultipartStage(t *testing.T) {
	root, db, svc := deleteFixture(t)
	ctx := context.Background()
	reader := &pausedStageReader{started: make(chan struct{}), proceed: make(chan struct{}), reader: strings.NewReader("ID3active-upload")}
	type result struct {
		stage *Stage
		err   error
	}
	completed := make(chan result, 1)
	go func() { stage, err := svc.Stage(ctx, reader, 1024); completed <- result{stage, err} }()
	<-reader.started
	other, err := New(root, db.Media(), nil)
	if err != nil {
		close(reader.proceed)
		t.Fatal(err)
	}
	defer other.Close()
	close(reader.proceed)
	value := <-completed
	if value.err != nil {
		t.Fatal(value.err)
	}
	defer value.stage.Close()
	if _, err := os.Stat(filepath.Join(root, value.stage.name)); err != nil {
		t.Fatal("worker startup removed active upload", err)
	}
	if _, err := svc.Publish(ctx, value.stage, "active.mp3", "music/artist", "", false, 240, store.AdminAudit{}); err != nil {
		t.Fatal("active publication failed after worker startup", err)
	}
}
