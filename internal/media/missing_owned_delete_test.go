package media

import (
	"context"
	"errors"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestMissingOwnedBytesDeleteUsesAccountedSizeAndRefundsOnce(t *testing.T) {
	root, db, svc, rel := ownedFixture(t)
	ctx := context.Background()
	o := store.MediaObject{ID: strings.Repeat("8", 64), Path: "music/MissingBytes/song.mp3", Kind: "audio"}
	if _, err := svc.OwnedUpload(ctx, rel, o, 10, strings.NewReader("ID3payload"), store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(root, o.Path)); err != nil {
		t.Fatal(err)
	}
	if err := svc.OwnedDelete(ctx, rel, o.ID, o.Path, 1, store.NodeAudit{}); !errors.Is(err, store.ErrNodeState) {
		t.Fatal("wrong missing-byte accounting accepted", err)
	}
	ownedFunds(t, db, 10, 0)
	for range 2 {
		if err := svc.OwnedDelete(ctx, rel, o.ID, o.Path, 10, store.NodeAudit{}); err != nil {
			t.Fatal(err)
		}
	}
	ownedFunds(t, db, 0, 0)
}
