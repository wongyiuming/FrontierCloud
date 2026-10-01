package node

import (
	"context"
	"crypto/x509"
	"io"
	"net/http"
	"strconv"
	"strings"
	"testing"
)

func TestMediaReadSeparateStreamingPoolPinsPlacementTLSAndHidesCapabilities(t *testing.T) {
	object, resource, owner := strings.Repeat("a", 64), strings.Repeat("b", 64), strings.Repeat("c", 32)
	token := "private-media-capability-do-not-log"
	data := strings.Repeat("x", 2*1024*1024)
	server, roots := privateCA(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Media-Capability") != token || r.Method != "GET" || r.URL.RawQuery != "" {
			t.Error("media download contract")
			w.WriteHeader(400)
			return
		}
		if strings.HasSuffix(r.URL.Path, strings.Repeat("d", 64)) {
			http.Redirect(w, r, "https://evil.test", 307)
			return
		}
		w.Header().Set("X-Media-Object-ID", object)
		w.Header().Set("X-Media-Resource-ID", resource)
		w.Header().Set("X-Media-Owner-ID", owner)
		w.Header().Set("Content-Length", strconv.Itoa(len(data)))
		w.Write([]byte(data))
	}))
	transport := testTransport(t, server, roots)
	if transport.storage == transport.client || transport.storage.Transport == transport.client.Transport {
		t.Fatal("download uses control pool")
	}
	reader, err := transport.MediaRead(context.Background(), "https://node.test", object, resource, owner, token, int64(len(data)))
	if err != nil {
		t.Fatal(err)
	}
	n, err := io.CopyBuffer(io.Discard, reader, make([]byte, 64*1024))
	reader.Close()
	if err != nil || n != int64(len(data)) {
		t.Fatal(n, err)
	}
	for _, test := range []struct {
		origin, id, resource, owner string
		size                        int64
	}{
		{"https://node.test", strings.Repeat("d", 64), resource, owner, int64(len(data))},
		{"https://node.test", object, strings.Repeat("e", 64), owner, int64(len(data))},
		{"https://node.test", object, resource, strings.Repeat("f", 32), int64(len(data))},
		{"https://node.test", object, resource, owner, 10},
		{"https://wrong-name.test", object, resource, owner, int64(len(data))},
		{"http://node.test", object, resource, owner, int64(len(data))},
		{"https://node.test", "../escape", resource, owner, int64(len(data))},
	} {
		reader, err := transport.MediaRead(context.Background(), test.origin, test.id, test.resource, test.owner, token, test.size)
		if reader != nil {
			reader.Close()
		}
		if err == nil || strings.Contains(err.Error(), token) {
			t.Fatal("unsafe media download accepted", err)
		}
	}
	untrusted := testTransport(t, server, x509.NewCertPool())
	if reader, err := untrusted.MediaRead(context.Background(), "https://node.test", object, resource, owner, token, int64(len(data))); err == nil {
		reader.Close()
		t.Fatal("untrusted media CA accepted")
	}
}
