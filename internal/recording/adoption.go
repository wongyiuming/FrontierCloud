package recording

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// ParseInventory requires the exact signed shape, unambiguous bounded JSON and
// no trailing data. Parsing alone is not an authenticity or ownership proof.
func ParseInventory(reader io.Reader) (store.SignedRecordingInventory, error) {
	var result store.SignedRecordingInventory
	raw, err := io.ReadAll(io.LimitReader(reader, store.MaxRecordingInventoryBytes+1))
	if err != nil {
		return result, err
	}
	if _, err = protocol.ParseStrictJSON(raw, store.MaxRecordingInventoryBytes); err != nil {
		return result, store.ErrRecordingState
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&result) != nil || result.Payload.Recordings == nil {
		return store.SignedRecordingInventory{}, store.ErrRecordingState
	}
	// The signature is over the full canonical typed payload. Omitting required
	// fields or inserting null cannot silently produce a different signed object.
	value, _ := protocol.ParseStrictJSON(raw, store.MaxRecordingInventoryBytes)
	envelope, ok := value.(map[string]any)
	if !ok || len(envelope) != 2 {
		return store.SignedRecordingInventory{}, store.ErrRecordingState
	}
	payload, err := result.Payload.CanonicalPayload()
	if err != nil {
		return store.SignedRecordingInventory{}, store.ErrRecordingState
	}
	canonical, err := protocol.Canonical(payload)
	if err != nil {
		return store.SignedRecordingInventory{}, store.ErrRecordingState
	}
	actual, err := protocol.Canonical(envelope["payload"])
	if err != nil || !bytes.Equal(canonical, actual) {
		return store.SignedRecordingInventory{}, store.ErrRecordingState
	}
	return result, nil
}

// AdoptOwnedRecordings never invokes startup recovery or modifies recording
// bytes. The caller must hold the closed native lifecycle fence; the operator
// must additionally isolate Python and remote writers not using these leases.
func AdoptOwnedRecordings(ctx context.Context, directory string, repo store.MaintenanceRepository, recordings store.RecordingRepository, nodes store.NodeRepository, signed store.SignedRecordingInventory, confirmation string) (int, error) {
	n, err := nodes.ReadIdentity(ctx)
	if err != nil {
		return 0, err
	}
	v := signed.Payload
	if n.Role != "Follower" || n.ID != confirmation || v.FollowerID != confirmation || !identifier.MatchString(v.Relationship) {
		return 0, store.ErrNodeState
	}
	rel, err := nodes.Relationship(ctx, v.Relationship)
	if err != nil {
		return 0, err
	}
	payload, err := v.CanonicalPayload()
	if err != nil {
		return 0, store.ErrRecordingState
	}
	if rel.State != "active" || rel.Direction != "upstream" || rel.Protocol != 2 || rel.PeerID != v.MasterID || protocol.Verify(rel.PublicKey, payload, signed.Signature) != nil {
		return 0, store.ErrNodeState
	}
	info, err := os.Lstat(directory)
	if err != nil {
		return 0, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return 0, ErrRecovery
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		return 0, err
	}
	defer root.Close()
	opened, err := root.Stat(".")
	if err != nil || !os.SameFile(info, opened) {
		return 0, ErrRecovery
	}
	s := &Storage{root: root, repo: recordings, nodes: nodes}
	release, err := s.acquire(ctx, true)
	if err != nil {
		return 0, err
	}
	defer release()
	if err = s.ready(); err != nil {
		return 0, err
	}
	if len(v.Recordings) > store.MaxRecordingInventoryItems {
		return 0, store.ErrRecordingState
	}
	expected := map[string]store.RecordingProof{}
	for _, p := range v.Recordings {
		name, e := Path(v.Relationship, p.UserID, p.ID)
		if e != nil {
			return 0, e
		}
		if _, duplicate := expected[name]; duplicate {
			return 0, store.ErrRecordingState
		}
		if p.Bytes <= 0 || p.Bytes > store.MaxRecordingBytes || len(p.SHA256) != 64 {
			return 0, store.ErrRecordingState
		}
		expected[name] = p
	}
	entries := 0
	err = fs.WalkDir(root.FS(), ".", func(name string, entry fs.DirEntry, walkErr error) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if walkErr != nil {
			return walkErr
		}
		entries++
		if entries > store.MaxRecordingInventoryItems*4+2 {
			return ErrRecovery
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return ErrRecovery
		}
		parts := strings.Split(name, "/")
		if entry.IsDir() {
			if name == "." {
				return nil
			}
			if len(parts) > 2 || parts[0] != v.Relationship || len(parts) == 2 && !identifier.MatchString(parts[1]) {
				return ErrRecovery
			}
			return nil
		}
		if name == ".recordings-mutation.lock" || len(parts) == 1 && strings.HasPrefix(name, ".recording-") && strings.HasSuffix(name, ".lease") && identifier.MatchString(strings.TrimSuffix(strings.TrimPrefix(name, ".recording-"), ".lease")) {
			info, e := entry.Info()
			if e != nil {
				return e
			}
			if !info.Mode().IsRegular() || info.Size() != 0 {
				return ErrRecovery
			}
			return nil
		}
		proof, ok := expected[name]
		if !ok {
			return ErrRecovery
		}
		before, e := s.info(name)
		if e != nil {
			return e
		}
		if before.Size() != proof.Bytes {
			return ErrSize
		}
		file, e := root.Open(name)
		if e != nil {
			return e
		}
		opened, e := file.Stat()
		if e != nil || !os.SameFile(before, opened) {
			file.Close()
			return ErrRecovery
		}
		digest := sha256.New()
		count, copyErr := io.CopyBuffer(digest, &inventoryReader{ctx, io.LimitReader(file, proof.Bytes+1)}, make([]byte, 64*1024))
		after, statErr := file.Stat()
		closeErr := file.Close()
		current, pathErr := s.info(name)
		if e = errors.Join(copyErr, statErr, closeErr, pathErr); e != nil {
			return e
		}
		if count != proof.Bytes || after.Size() != before.Size() || !before.ModTime().Equal(after.ModTime()) || !os.SameFile(before, current) || !before.ModTime().Equal(current.ModTime()) || hex.EncodeToString(digest.Sum(nil)) != proof.SHA256 {
			return ErrRecovery
		}
		delete(expected, name)
		return nil
	})
	if err != nil {
		return 0, err
	}
	if len(expected) != 0 {
		return 0, ErrRecovery
	}
	return repo.AdoptOwnedRecordings(ctx, signed, confirmation, store.NodeAudit{Actor: "offline-recording-adoption"})
}

type inventoryReader struct {
	ctx context.Context
	io.Reader
}

func (r *inventoryReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.Reader.Read(p)
}
