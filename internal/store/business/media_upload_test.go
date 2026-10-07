package business_test

import (
	"context"
	"database/sql"
	"strings"
	"sync"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestUploadPublicationReplayIsAtomicAndIdempotent(t *testing.T) {
	db := database(t)
	ctx := context.Background()
	object := store.MediaObject{Path: "music/" + t.Name() + "/song.mp3", Kind: "audio"}
	audit := store.AdminAudit{Action: "upload_item", RequestID: "upload-publication-test"}
	var workers sync.WaitGroup
	failures := make(chan error, 16)
	for range 16 {
		workers.Go(func() {
			if err := db.Media().CompleteUpload(ctx, object, strings.Repeat("c", 32), audit); err != nil {
				failures <- err
			}
		})
	}
	workers.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	var count int
	if err := db.(interface{ Database() *sql.DB }).Database().QueryRow("SELECT COUNT(*) FROM admin_audit_log WHERE request_id=?", "upload-publication-test").Scan(&count); err != nil || count != 1 {
		t.Fatalf("publication replay: %d audits %v", count, err)
	}
}
