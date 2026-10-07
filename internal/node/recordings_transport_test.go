package node

import (
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestRecordingTransportVerifiedPrivateCASeparatePoolAndLargeBoundedMetadata(t *testing.T) {
	id := strings.Repeat("a", 32)
	token := "recording-secret-do-not-log"
	payload := strings.Repeat("x", 2*1024*1024)
	server, roots := privateCA(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/internal/v1/recordings/"+strings.Repeat("b", 32) {
			w.WriteHeader(409)
			w.Write([]byte(token))
			return
		}
		if r.Method != "PUT" || r.Header.Get("X-Recording-Capability") != token || r.Header.Get("Content-Type") != "audio/webm" || r.ContentLength != int64(len(payload)) {
			t.Error("recording request contract")
			w.WriteHeader(400)
			return
		}
		hash := sha256.New()
		n, e := io.CopyBuffer(hash, r.Body, make([]byte, 64*1024))
		if e != nil || n != int64(len(payload)) {
			t.Error(n, e)
			w.WriteHeader(400)
			return
		}
		switch r.URL.Path {
		case "/internal/v1/recordings/" + strings.Repeat("c", 32):
			w.Write([]byte(strings.Repeat("x", 5*1024*1024+1)))
		case "/internal/v1/recordings/" + strings.Repeat("d", 32):
			http.Redirect(w, r, "https://evil.test", 307)
		default:
			json.NewEncoder(w).Encode(map[string]any{"size_bytes": n, "sha256": hex.EncodeToString(hash.Sum(nil)), "metadata": map[string]any{"title": "大歌词", "lyrics": []any{map[string]any{"time": 0, "text": strings.Repeat("x", 600*1024)}}}})
		}
	}))
	transport := testTransport(t, server, roots)
	if transport.storage == transport.client || transport.storage.Transport == transport.client.Transport {
		t.Fatal("recording uploaded through control pool")
	}
	value, e := transport.RecordingUpload(context.Background(), "https://node.test", id, token, "audio/webm", strings.NewReader(payload), int64(len(payload)))
	if e != nil || value["sha256"] == nil {
		t.Fatal(value, e)
	}
	for _, badID := range []string{strings.Repeat("b", 32), strings.Repeat("c", 32), strings.Repeat("d", 32), "../" + id} {
		if _, e = transport.RecordingUpload(context.Background(), "https://node.test", badID, token, "audio/webm", strings.NewReader(payload), int64(len(payload))); e == nil || strings.Contains(e.Error(), token) {
			t.Fatal("unsafe recording response", e)
		}
	}
	for _, origin := range []string{"https://wrong-name.test", "http://node.test", "https://127.0.0.1"} {
		if _, e = transport.RecordingUpload(context.Background(), origin, id, token, "audio/webm", strings.NewReader(payload), int64(len(payload))); e == nil || strings.Contains(e.Error(), token) {
			t.Fatal("unsafe recording TLS", e)
		}
	}
	untrusted := testTransport(t, server, x509.NewCertPool())
	if _, e = untrusted.RecordingUpload(context.Background(), "https://node.test", id, token, "audio/webm", strings.NewReader(payload), int64(len(payload))); e == nil {
		t.Fatal("untrusted recording CA")
	}
}
