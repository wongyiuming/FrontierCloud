package protocol

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"testing"
)

const vectorRoot = "../../protocol/v2/vectors"

func loadVector(t *testing.T, name string, target any) {
	t.Helper()
	content, err := os.ReadFile(filepath.Join(vectorRoot, name))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(content, target); err != nil {
		t.Fatal(err)
	}
}

func canonicalEqual(t *testing.T, actual, expected any) {
	t.Helper()
	actualBytes, err := Canonical(actual)
	if err != nil {
		t.Fatal(err)
	}
	expectedBytes, err := Canonical(expected)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(actualBytes, expectedBytes) {
		t.Fatalf("canonical values differ:\nactual:   %s\nexpected: %s", actualBytes, expectedBytes)
	}
}

func TestCanonicalVectors(t *testing.T) {
	var fixture struct {
		Protocol int `json:"protocol"`
		Cases    []struct {
			Name      string `json:"name"`
			Value     any    `json:"value"`
			Canonical string `json:"canonical"`
		} `json:"cases"`
	}
	loadVector(t, "canonical-json.json", &fixture)
	if fixture.Protocol != Version {
		t.Fatalf("fixture protocol %d does not match %d", fixture.Protocol, Version)
	}
	for _, testCase := range fixture.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			actual, err := Canonical(testCase.Value)
			if err != nil {
				t.Fatal(err)
			}
			if string(actual) != testCase.Canonical {
				t.Fatalf("got %s, want %s", actual, testCase.Canonical)
			}
		})
	}
	for _, invalid := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		if _, err := Canonical(invalid); err == nil {
			t.Fatal("non-finite number was accepted")
		}
	}
}

func TestEd25519Vector(t *testing.T) {
	var fixture struct {
		PrivateKey string         `json:"private_key"`
		PublicKey  string         `json:"public_key"`
		Payload    map[string]any `json:"payload"`
		Canonical  string         `json:"canonical"`
		Signature  string         `json:"signature"`
	}
	loadVector(t, "ed25519.json", &fixture)
	public, err := PublicKey(fixture.PrivateKey)
	if err != nil {
		t.Fatal(err)
	}
	if public != fixture.PublicKey {
		t.Fatalf("got public key %s, want %s", public, fixture.PublicKey)
	}
	canonical, err := Canonical(fixture.Payload)
	if err != nil {
		t.Fatal(err)
	}
	if string(canonical) != fixture.Canonical {
		t.Fatalf("got canonical payload %s, want %s", canonical, fixture.Canonical)
	}
	signature, err := Sign(fixture.PrivateKey, fixture.Payload)
	if err != nil {
		t.Fatal(err)
	}
	if signature != fixture.Signature {
		t.Fatalf("got signature %s, want %s", signature, fixture.Signature)
	}
	if err := Verify(fixture.PublicKey, fixture.Payload, fixture.Signature); err != nil {
		t.Fatal(err)
	}
}

func TestNodeAuthVector(t *testing.T) {
	var fixture struct {
		Credential   string `json:"credential"`
		Relationship string `json:"relationship"`
		Timestamp    string `json:"timestamp"`
		Nonce        string `json:"nonce"`
		Method       string `json:"method"`
		Path         string `json:"path"`
		Body         string `json:"body"`
		BodySHA256   string `json:"body_sha256"`
		SigningInput string `json:"signing_input"`
		SignatureHex string `json:"signature_hex"`
	}
	loadVector(t, "node-auth.json", &fixture)
	digest := sha256.Sum256([]byte(fixture.Body))
	if hex.EncodeToString(digest[:]) != fixture.BodySHA256 {
		t.Fatal("body digest does not match fixture")
	}
	headers := map[string]string{
		"X-Node-Relationship": fixture.Relationship,
		"X-Node-Time":         fixture.Timestamp,
		"X-Node-Nonce":        fixture.Nonce,
		"X-Node-Signature":    fixture.SignatureHex,
	}
	nonce, err := VerifyAuth(fixture.Credential, headers, fixture.Method, fixture.Path, []byte(fixture.Body), 1700000000)
	if err != nil {
		t.Fatal(err)
	}
	if nonce != fixture.Nonce {
		t.Fatalf("got nonce %s, want %s", nonce, fixture.Nonce)
	}
}

func TestMediaTokenVector(t *testing.T) {
	var fixture struct {
		Credential string `json:"credential"`
		IssuedAt   int64  `json:"issued_at"`
		VerifyAt   int64  `json:"verify_at"`
		Arguments  struct {
			Relationship string `json:"relationship"`
			Master       string `json:"master"`
			Owner        string `json:"owner"`
			Original     string `json:"original"`
			MediaID      string `json:"media_id"`
			RequestID    string `json:"request_id"`
			TraceID      string `json:"trace_id"`
		} `json:"arguments"`
		Payload map[string]any `json:"payload"`
		Token   string         `json:"token"`
	}
	loadVector(t, "media-token.json", &fixture)
	token, err := MediaToken(fixture.Credential, fixture.Arguments.Relationship, fixture.Arguments.Master,
		fixture.Arguments.Owner, fixture.Arguments.Original, fixture.Arguments.MediaID, fixture.IssuedAt,
		fixture.Arguments.RequestID, fixture.Arguments.TraceID)
	if err != nil {
		t.Fatal(err)
	}
	if token != fixture.Token {
		t.Fatalf("media token differs:\n%s\n%s", token, fixture.Token)
	}
	payload, err := VerifyMediaToken(fixture.Credential, token, fixture.VerifyAt)
	if err != nil {
		t.Fatal(err)
	}
	canonicalEqual(t, payload, fixture.Payload)
}

func TestStorageTokenVector(t *testing.T) {
	var fixture struct {
		Credential string `json:"credential"`
		IssuedAt   int64  `json:"issued_at"`
		VerifyAt   int64  `json:"verify_at"`
		Arguments  struct {
			Relationship string `json:"relationship"`
			Master       string `json:"master"`
			StorageNode  string `json:"storage_node"`
			MediaID      string `json:"media_id"`
			ObjectID     string `json:"object_id"`
			Operation    string `json:"operation"`
			Path         string `json:"path"`
			Size         int64  `json:"size"`
		} `json:"arguments"`
		Payload map[string]any `json:"payload"`
		Token   string         `json:"token"`
	}
	loadVector(t, "storage-token.json", &fixture)
	token, err := StorageToken(fixture.Credential, fixture.Arguments.Relationship, fixture.Arguments.Master,
		fixture.Arguments.StorageNode, fixture.Arguments.MediaID, fixture.Arguments.ObjectID,
		fixture.Arguments.Operation, fixture.Arguments.Path, fixture.Arguments.Size, fixture.IssuedAt)
	if err != nil {
		t.Fatal(err)
	}
	if token != fixture.Token {
		t.Fatalf("storage token differs:\n%s\n%s", token, fixture.Token)
	}
	payload, err := VerifyStorageToken(fixture.Credential, token, fixture.VerifyAt)
	if err != nil {
		t.Fatal(err)
	}
	canonicalEqual(t, payload, fixture.Payload)
}

func TestRecordingTokenVector(t *testing.T) {
	var fixture struct {
		Credential string `json:"credential"`
		IssuedAt   int64  `json:"issued_at"`
		VerifyAt   int64  `json:"verify_at"`
		Arguments  struct {
			Relationship string `json:"relationship"`
			Master       string `json:"master"`
			Owner        string `json:"owner"`
			Recording    string `json:"recording"`
			Operation    string `json:"operation"`
			Size         int64  `json:"size"`
			ContentType  string `json:"content_type"`
			Filename     string `json:"filename"`
		} `json:"arguments"`
		Payload map[string]any `json:"payload"`
		Token   string         `json:"token"`
	}
	loadVector(t, "recording-token.json", &fixture)
	token, err := RecordingToken(fixture.Credential, fixture.Arguments.Relationship, fixture.Arguments.Master,
		fixture.Arguments.Owner, fixture.Arguments.Recording, fixture.Arguments.Operation, fixture.IssuedAt,
		fixture.Arguments.Size, fixture.Arguments.ContentType, fixture.Arguments.Filename)
	if err != nil {
		t.Fatal(err)
	}
	if token != fixture.Token {
		t.Fatalf("recording token differs:\n%s\n%s", token, fixture.Token)
	}
	payload, err := VerifyRecordingToken(fixture.Credential, token, fixture.VerifyAt)
	if err != nil {
		t.Fatal(err)
	}
	canonicalEqual(t, payload, fixture.Payload)
}
