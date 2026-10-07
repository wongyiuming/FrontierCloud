package node

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestCapabilitiesBindRoleRelationshipOwnerObjectAndOperation(t *testing.T) {
	ctx := context.Background()
	transport := &localControl{nodes: map[string]*Service{}}
	master, _ := testNode(t, "Master", "https://master.test", transport)
	follower, _ := testNode(t, "Follower", "https://follower.test", transport)
	transport.nodes["https://master.test"] = master
	transport.nodes["https://follower.test"] = follower
	packageValue, err := follower.CreatePair(ctx, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	id, err := master.ImportPair(ctx, packageValue, store.NodeAudit{})
	if err != nil {
		t.Fatal(err)
	}
	relation, _ := master.repo.Relationship(ctx, id)
	objectID, resourceID := strings.Repeat("a", 64), strings.Repeat("b", 64)
	token, err := master.MediaCapability(ctx, id, relation.PeerID, objectID, resourceID, strings.Repeat("c", 32), strings.Repeat("d", 32))
	if err != nil {
		t.Fatal(err)
	}
	verified, payload, err := follower.VerifyOwnedMedia(ctx, token, objectID)
	if err != nil || verified.ID != id || payload["g"] != resourceID || payload["request_id"] != strings.Repeat("c", 32) {
		t.Fatal("media capability", payload, err)
	}
	if _, _, err = follower.VerifyOwnedMedia(ctx, token, strings.Repeat("e", 64)); !errors.Is(err, ErrCapability) {
		t.Fatal("wrong object accepted", err)
	}
	if _, _, err = master.VerifyOwnedMedia(ctx, token, objectID); !errors.Is(err, ErrCapability) {
		t.Fatal("Master accepted a Follower capability", err)
	}
	if _, err = master.MediaCapability(ctx, id, strings.Repeat("f", 32), objectID, resourceID, "", ""); !errors.Is(err, ErrCapability) {
		t.Fatal("wrong owner signed", err)
	}
	v := store.UploadReservation{ID: strings.Repeat("e", 32), MemberID: relation.PeerID, MediaID: objectID, Path: "music/Capability/track.mp3", ExpectedBytes: 7, Member: store.StorageMember{RelationshipID: &id}}
	storage, err := master.StorageCapability(ctx, v, "upload")
	if err != nil {
		t.Fatal(err)
	}
	_, payload, err = follower.VerifyOwnedStorage(ctx, storage, objectID, "upload")
	if err != nil || payload["path"] != v.Path {
		t.Fatal("storage capability", payload, err)
	}
	if _, _, err = follower.VerifyOwnedStorage(ctx, storage, objectID, "delete"); !errors.Is(err, ErrCapability) {
		t.Fatal("upload token allowed deletion", err)
	}
	if _, _, err = follower.VerifyOwnedMedia(ctx, storage, objectID); !errors.Is(err, ErrCapability) {
		t.Fatal("storage token allowed streaming", err)
	}
	if err = follower.repo.RevokeRelationship(ctx, id, true, store.NodeAudit{}); err != nil {
		t.Fatal(err)
	}
	if _, _, err = follower.VerifyOwnedMedia(ctx, token, objectID); !errors.Is(err, ErrCapability) {
		t.Fatal("revoked token allowed streaming", err)
	}
	if _, _, err = follower.VerifyOwnedStorage(ctx, storage, objectID, "upload"); !errors.Is(err, ErrCapability) {
		t.Fatal("revoked token allowed upload", err)
	}
}
