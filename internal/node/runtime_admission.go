package node

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"strings"

	"github.com/wongyiuming/FrontierCloud/internal/deployment"
	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/fsutil"
	"github.com/wongyiuming/FrontierCloud/internal/maintenance"
)

const runtimeReceipt = ".native-runtime"
const runtimeReceiptLock = ".native-admission.lock"

var ErrRuntimeAdmission = errors.New("native cluster admission requires a completed native startup or verified offline migration")

// CheckNativeRuntime admits previously native roles, never an unproven legacy
// cluster. The receipt is private local provenance, not a protocol capability or
// a substitute for media/recording recovery on EVERY startup.
func (n *Identity) CheckNativeRuntime(ctx context.Context, directory string) error {
	return n.runtimeAdmission(ctx, directory, false)
}

// RecordNativeRuntime is called only after all production services and recovery
// have initialized, before HTTP starts. Standalone reset may replace a valid old
// identity receipt; malformed/foreign receipts are never overwritten.
func (n *Identity) RecordNativeRuntime(ctx context.Context, directory string) error {
	return n.runtimeAdmission(ctx, directory, true)
}

// AdmitVerifiedMaster is an offline operator boundary, not a startup option.
// The verifier must acquire an authoritative database write fence and compare
// the full stopped source with a restored recovery artifact. Its release is
// held through durable receipt publication. A closed local maintenance gate
// alone does NOT fence legacy/remote writers. Neither nil nor failed proof can
// grant authority, and existing foreign/malformed receipts remain fail-closed.
func (n *Identity) AdmitVerifiedMaster(ctx context.Context, directory string, verify func(context.Context) (func(), error)) error {
	if n == nil || n.Role != "Master" || verify == nil {
		return ErrRuntimeAdmission
	}
	gate, err := maintenance.Open(directory)
	if err != nil {
		return err
	}
	defer gate.Close()
	return gate.Inspect(ctx, func(ctx context.Context) error {
		release, err := verify(ctx)
		if release != nil {
			defer release()
		}
		if err != nil {
			return err
		}
		if release == nil {
			return ErrRuntimeAdmission
		}
		return n.publishAdmission(ctx, directory, true, true)
	})
}

func (n *Identity) runtimeAdmission(ctx context.Context, directory string, publish bool) error {
	return n.publishAdmission(ctx, directory, publish, false)
}

func (n *Identity) publishAdmission(ctx context.Context, directory string, publish, verifiedMaster bool) error {
	if n == nil || n.vault == nil || !ValidIdentifier(n.ID) || (n.Role != "Standalone" && n.Role != "Master" && n.Role != "Follower") {
		return ErrRuntimeAdmission
	}
	info, err := os.Lstat(directory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return ErrRuntimeAdmission
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		return err
	}
	defer root.Close()
	opened, err := root.Stat(".")
	if err != nil || !os.SameFile(info, opened) {
		return ErrRuntimeAdmission
	}
	read := func(name string, limit int64) ([]byte, error) {
		info, err := root.Lstat(name)
		if err != nil {
			return nil, err
		}
		if !info.Mode().IsRegular() || info.Size() <= 0 || info.Size() > limit {
			return nil, ErrRuntimeAdmission
		}
		f, err := root.Open(name)
		if err != nil {
			return nil, err
		}
		defer f.Close()
		opened, err := f.Stat()
		if err != nil || !os.SameFile(info, opened) {
			return nil, ErrRuntimeAdmission
		}
		return io.ReadAll(io.LimitReader(f, limit+1))
	}
	lockName := runtimeReceiptLock
	info, err = root.Lstat(lockName)
	if err == nil && (!info.Mode().IsRegular() || info.Size() != 0) {
		return ErrRuntimeAdmission
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	f, err := root.OpenFile(lockName, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	opened, err = f.Stat()
	info, found := root.Lstat(lockName)
	if err != nil || found != nil || !info.Mode().IsRegular() || info.Size() != 0 || !os.SameFile(info, opened) {
		return ErrRuntimeAdmission
	}
	if err = fsutil.InheritOwner(f, root); err != nil {
		return err
	}
	done, err := filelease.Acquire(ctx, f, true)
	if err != nil {
		return err
	}
	defer done()
	binding, err := read(deployment.Binding, 256)
	if err != nil || !strings.HasPrefix(string(binding), "native-store-v1:") {
		return ErrRuntimeAdmission
	}
	prefix := "frontiercloud-native-runtime-v1\n" + string(binding)
	raw, err := read(runtimeReceipt, 4096)
	if errors.Is(err, os.ErrNotExist) {
		if n.Role != "Standalone" && !(verifiedMaster && n.Role == "Master") {
			return ErrRuntimeAdmission
		}
	} else if err != nil {
		return err
	} else {
		proof, err := n.vault.Unseal(string(raw))
		if err != nil || !strings.HasPrefix(proof, prefix) || !ValidIdentifier(strings.TrimPrefix(proof, prefix)) {
			return ErrRuntimeAdmission
		}
		if proof == prefix+n.ID {
			return fsutil.SyncDirectory(root, ".")
		}
		if n.Role != "Standalone" {
			return ErrRuntimeAdmission
		}
	}
	if !publish {
		return nil
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	sealed, err := n.vault.Seal(prefix + n.ID)
	if err != nil {
		return err
	}
	var nonce [16]byte
	if _, err = rand.Read(nonce[:]); err != nil {
		return err
	}
	name := runtimeReceipt + "-write-" + hex.EncodeToString(nonce[:])
	file, err := root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer root.Remove(name)
	if err = fsutil.InheritOwner(file, root); err == nil {
		_, err = file.WriteString(sealed)
	}
	if err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err != nil || closeErr != nil {
		return errors.Join(err, closeErr)
	}
	if err = root.Rename(name, runtimeReceipt); err != nil {
		return err
	}
	return fsutil.SyncDirectory(root, ".")
}
