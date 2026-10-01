package node

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
)

func TestEndpointRejectsNonRootAndUnsafeAddresses(t *testing.T) {
	for _, value := range []string{"http://192.168.6.201", "https://localhost", "https://127.0.0.1", "https://169.254.169.254", "https://224.0.0.1", "https://0.0.0.0", "https://[::1]", "https://user:password@node.test", "https://node.test/path", "https://node.test/?secret=abc", "https://node.test/#fragment", "https://node.test?", "https://node.test:0", "https://node.test:65536", "https://metadata.google.internal", "https://test.localhost"} {
		if _, err := Endpoint(value); err == nil {
			t.Fatalf("accepted endpoint %s", value)
		}
	}
	for value, want := range map[string]string{"https://NODE.test:443/": "https://node.test", "https://192.168.6.201:14443": "https://192.168.6.201:14443", "https://例子.测试/": "https://xn--fsqu00a.xn--0zwm56d"} {
		got, err := Endpoint(value)
		if err != nil || got != want {
			t.Fatalf("%s => %s %v", value, got, err)
		}
	}
	if _, err := safeDial(context.Background(), "tcp", "127.0.0.1:443"); err == nil {
		t.Fatal("dial permitted loopback DNS result")
	}
}

func TestStorageUploadUsesSeparateStreamingPoolVerifiedTLSAndBoundedReceipt(t *testing.T) {
	payload := "ID3" + strings.Repeat("x", 2*1024*1024)
	id := strings.Repeat("a", 64)
	token := "capability-do-not-log"
	server, roots := privateCA(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "PUT" || r.Header.Get("X-Storage-Capability") != token || r.ContentLength != int64(len(payload)) {
			t.Error("storage request contract")
			w.WriteHeader(400)
			return
		}
		if r.URL.Path != "/internal/v1/storage/"+id {
			w.WriteHeader(409)
			w.Write([]byte(token))
			return
		}
		hash := sha256.New()
		n, err := io.CopyBuffer(hash, r.Body, make([]byte, 64*1024))
		if err != nil || n != int64(len(payload)) {
			t.Error(n, err)
			w.WriteHeader(400)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"object_id": id, "size_bytes": n, "sha256": hex.EncodeToString(hash.Sum(nil))})
	}))
	transport := testTransport(t, server, roots)
	if transport.storage == transport.client || transport.storage.Transport == transport.client.Transport || transport.storage.Timeout != 0 {
		t.Fatal("media uses control connection pool/timeout")
	}
	result, err := transport.StorageUpload(context.Background(), "https://node.test", id, token, strings.NewReader(payload), int64(len(payload)))
	if err != nil || result["object_id"] != id {
		t.Fatal(result, err)
	}
	for _, origin := range []string{"https://wrong-name.test", "http://node.test", "https://127.0.0.1"} {
		if _, err := transport.StorageUpload(context.Background(), origin, id, token, strings.NewReader(payload), int64(len(payload))); err == nil || strings.Contains(err.Error(), token) {
			t.Fatal("unsafe TLS or secret error", err)
		}
	}
	if _, err := transport.StorageUpload(context.Background(), "https://node.test", strings.Repeat("b", 64), token, strings.NewReader(payload), int64(len(payload))); err == nil || strings.Contains(err.Error(), token) {
		t.Fatal("unsafe error body", err)
	}
	untrusted := testTransport(t, server, x509.NewCertPool())
	if _, err := untrusted.StorageUpload(context.Background(), "https://node.test", id, token, strings.NewReader(payload), int64(len(payload))); err == nil {
		t.Fatal("untrusted storage CA accepted")
	}
}

func privateCA(t *testing.T, handler http.Handler) (*httptest.Server, *x509.CertPool) {
	t.Helper()
	pub, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	caTemplate := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "FrontierCloud test CA"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	caDER, err := x509.CreateCertificate(rand.Reader, caTemplate, caTemplate, pub, key)
	if err != nil {
		t.Fatal(err)
	}
	ca, err := x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatal(err)
	}
	serverPublic, serverKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	certificate := &x509.Certificate{SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "node.test"}, DNSNames: []string{"node.test"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, KeyUsage: x509.KeyUsageDigitalSignature}
	der, err := x509.CreateCertificate(rand.Reader, certificate, ca, serverPublic, key)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(handler)
	server.TLS = &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{der, caDER}, PrivateKey: serverKey}}, MinVersion: tls.VersionTLS12}
	server.StartTLS()
	t.Cleanup(server.Close)
	roots := x509.NewCertPool()
	roots.AddCert(ca)
	return server, roots
}
func testTransport(t *testing.T, server *httptest.Server, roots *x509.CertPool) *Transport {
	t.Helper()
	transport := transportWithRoots(roots)
	for _, client := range []*http.Client{transport.client, transport.backup, transport.storage} {
		client.Transport.(*http.Transport).DialContext = func(ctx context.Context, network, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, server.Listener.Addr().String())
		}
	}
	t.Cleanup(transport.Close)
	return transport
}
func TestControlPrivateCAIdentityPinningBoundedBodiesAndNoRedirects(t *testing.T) {
	ctx := context.Background()
	private := protocol.Encode(bytes.Repeat([]byte{7}, 32))
	public, err := protocol.PublicKey(private)
	if err != nil {
		t.Fatal(err)
	}
	id := strings.Repeat("1", 32)
	credential := protocol.Encode(bytes.Repeat([]byte{8}, 48))
	relation := strings.Repeat("2", 32)
	server, roots := privateCA(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/internal/v1/identity":
			payload := map[string]any{"node_id": id, "role": "Follower", "endpoint": "https://node.test", "public_key": public, "challenge": r.URL.Query().Get("challenge"), "protocol": 2, "app_version": AppVersion}
			signature, err := protocol.Sign(private, payload)
			if err != nil {
				t.Error(err)
				w.WriteHeader(500)
				return
			}
			json.NewEncoder(w).Encode(Envelope{payload, signature})
		case "/internal/v1/control":
			body, err := io.ReadAll(r.Body)
			if err != nil {
				t.Error(err)
				w.WriteHeader(500)
				return
			}
			headers := map[string]string{}
			for k, v := range r.Header {
				if len(v) == 1 {
					headers[k] = v[0]
				}
			}
			if _, err := protocol.VerifyAuth(credential, headers, r.Method, r.URL.RequestURI(), body, time.Now().Unix()); err != nil {
				t.Error(err)
				w.WriteHeader(401)
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
		case "/internal/v1/redirect":
			http.Redirect(w, r, "https://attacker.test/secret", http.StatusFound)
		case "/internal/v1/large":
			w.Write([]byte(strings.Repeat("x", MaxControlBytes+1)))
		case "/internal/v1/trailing":
			w.Write([]byte(`{"ok":true}{"evil":true}`))
		case "/internal/v1/secret-error":
			w.WriteHeader(409)
			w.Write([]byte("PRIVATE-CREDENTIAL-DO-NOT-LOG"))
		default:
			w.WriteHeader(404)
		}
	}))
	transport := testTransport(t, server, roots)
	peer, err := transport.Identity(ctx, "https://node.test", id, public, "Follower")
	if err != nil || peer.ID != id {
		t.Fatalf("private CA identity %+v %v", peer, err)
	}
	if _, err = transport.Identity(ctx, "https://node.test", strings.Repeat("3", 32), public, "Follower"); err == nil {
		t.Fatal("identity ID mismatch accepted")
	}
	if _, err = transport.Identity(ctx, "https://node.test", id, public, "Master"); err == nil {
		t.Fatal("role mismatch accepted")
	}
	wrongPublic, _ := protocol.PublicKey(protocol.Encode(bytes.Repeat([]byte{9}, 32)))
	if _, err = transport.Identity(ctx, "https://node.test", id, wrongPublic, "Follower"); err == nil {
		t.Fatal("untrusted signing key accepted")
	}
	untrusted := testTransport(t, server, x509.NewCertPool())
	if _, err = untrusted.Identity(ctx, "https://node.test", id, public, "Follower"); err == nil {
		t.Fatal("untrusted CA accepted")
	}
	if _, err = transport.Request(ctx, "https://wrong-name.test", "/internal/v1/identity", "GET", nil, "", ""); err == nil {
		t.Fatal("TLS hostname mismatch accepted")
	}
	value, err := transport.Request(ctx, "https://node.test", "/internal/v1/control?scope=test", "POST", map[string]any{"value": "中文"}, relation, credential)
	if err != nil || value["status"] != "ok" {
		t.Fatal("HMAC request", value, err)
	}
	for _, route := range []string{"/internal/v1/redirect", "/internal/v1/large", "/internal/v1/trailing", "/internal/v1/secret-error", "https://node.test/internal/v1/identity", "/internal/v1/../identity", "/internal/v1/%69dentity", "/public"} {
		if _, err = transport.Request(ctx, "https://node.test", route, "GET", nil, "", ""); err == nil || strings.Contains(err.Error(), "PRIVATE-CREDENTIAL") {
			t.Fatalf("unsafe response/path %s: %v", route, err)
		}
	}
	if _, err = transport.Request(ctx, "https://node.test", "/internal/v1/control", "POST", map[string]any{"large": strings.Repeat("x", MaxControlBytes)}, "", ""); err == nil {
		t.Fatal("oversized outbound control accepted")
	}
}
