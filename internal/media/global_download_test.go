package media

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"strings"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestGlobalDownloadCopyWithholdsFinalBlockUntilSizeDigestAndEOFAreProved(t *testing.T) {
	data := strings.Repeat("x", 128*1024+13)
	hash := sha256.Sum256([]byte(data))
	row := store.GlobalMedia{Bytes: int64(len(data)), ETag: `"` + hex.EncodeToString(hash[:]) + `"`}
	download := &Download{ctx: context.Background()}
	for _, test := range []struct {
		name, payload string
		row           store.GlobalMedia
		good          bool
	}{
		{"valid", data, row, true},
		{"short", data[:len(data)-1], row, false},
		{"extra", data + "x", row, false},
		{"digest", strings.Repeat("y", len(data)), row, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			var output bytes.Buffer
			err := download.copyGlobal(&output, strings.NewReader(test.payload), test.row, make([]byte, 64*1024))
			if test.good {
				if err != nil || output.String() != data {
					t.Fatal("verified download", err)
				}
				return
			}
			if err == nil || int64(output.Len()) >= row.Bytes {
				t.Fatal("failed stream appeared complete", output.Len(), err)
			}
		})
	}
	if err := download.copyGlobal(shortArchiveWriter{}, strings.NewReader(data), row, make([]byte, 64*1024)); !errors.Is(err, io.ErrShortWrite) {
		t.Fatal("short writer accepted", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	download.ctx = ctx
	if err := download.copyGlobal(io.Discard, strings.NewReader(data), row, make([]byte, 64*1024)); !errors.Is(err, context.Canceled) {
		t.Fatal("cancel ignored", err)
	}
}

type shortArchiveWriter struct{}

func (shortArchiveWriter) Write(data []byte) (int, error) { return len(data) - 1, nil }
