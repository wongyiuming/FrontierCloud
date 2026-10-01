package node

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"path"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"golang.org/x/net/idna"
)

const AppVersion = "2.0.0rc0"
const MaxControlBytes = 512 * 1024

var nodeIdentifier = regexp.MustCompile(`^[a-f0-9]{32}$`)
var resourceIdentifier = regexp.MustCompile(`^[a-f0-9]{64}$`)

func ValidIdentifier(value string) bool { return nodeIdentifier.MatchString(value) }

var hostPattern = regexp.MustCompile(`^[a-z0-9.-]+$`)

func permittedAddress(address netip.Addr) bool {
	address = address.Unmap()
	return address.IsValid() && !address.IsLoopback() && !address.IsLinkLocalUnicast() && !address.IsLinkLocalMulticast() && !address.IsMulticast() && !address.IsUnspecified()
}

// Endpoint permits private LAN hosts but never local/metadata targets, credentials,
// paths or downgrade to HTTP. DNS is checked again against actual dial addresses.
func Endpoint(raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.Opaque != "" || (u.Path != "" && u.Path != "/") {
		return "", errors.New("节点通信需要可验证证书的 HTTPS 根地址")
	}
	host, err := idna.Lookup.ToASCII(strings.ToLower(u.Hostname()))
	if err != nil || !hostPattern.MatchString(host) || host == "localhost" || host == "metadata.google.internal" || strings.HasSuffix(host, ".localhost") {
		return "", errors.New("节点地址必须是有效的 HTTPS 主机名或 IPv4 地址")
	}
	if address, err := netip.ParseAddr(host); err == nil && !permittedAddress(address) {
		return "", errors.New("不能使用回环、链路本地或元数据地址")
	}
	port := u.Port()
	if port != "" {
		number, err := strconv.Atoi(port)
		if err != nil || number < 1 || number > 65535 {
			return "", errors.New("invalid endpoint port")
		}
		if number == 443 {
			port = ""
		} else {
			port = strconv.Itoa(number)
		}
	}
	if port != "" {
		host += ":" + port
	}
	return "https://" + host, nil
}

type Envelope struct {
	Payload   map[string]any `json:"payload"`
	Signature string         `json:"signature"`
}

func (e Envelope) wire() map[string]any {
	return map[string]any{"payload": e.Payload, "signature": e.Signature}
}

type Peer struct {
	ID         string
	Endpoint   string
	PublicKey  string
	Role       string
	AppVersion string
}
type ControlClient interface {
	Request(context.Context, string, string, string, any, string, string) (map[string]any, error)
	Identity(context.Context, string, string, string, string) (Peer, error)
}
type StorageClient interface {
	StorageUpload(context.Context, string, string, string, io.Reader, int64) (map[string]any, error)
}
type RecordingClient interface {
	RecordingUpload(context.Context, string, string, string, string, io.Reader, int64) (map[string]any, error)
}
type Transport struct{ client, backup, storage *http.Client }

func NewTransport() *Transport { return transportWithRoots(nil) }
func transportWithRoots(roots *x509.CertPool) *Transport {
	makeClient := func(limit int, timeout time.Duration) *http.Client {
		transport := &http.Transport{Proxy: nil, DisableCompression: true, ForceAttemptHTTP2: true, MaxConnsPerHost: limit, MaxIdleConns: limit, MaxIdleConnsPerHost: limit, IdleConnTimeout: 60 * time.Second, TLSHandshakeTimeout: 8 * time.Second, ResponseHeaderTimeout: timeout, TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots}}
		transport.DialContext = safeDial
		return &http.Client{Transport: transport, Timeout: timeout, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	}
	storage := makeClient(4, 30*time.Second)
	storage.Timeout = 0 // Large media uses a separate pool and caller cancellation.
	return &Transport{makeClient(4, 10*time.Second), makeClient(1, 60*time.Second), storage}
}
func safeDial(ctx context.Context, network, address string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return nil, err
	}
	addresses, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil {
		return nil, err
	}
	if len(addresses) == 0 {
		return nil, errors.New("node address not resolved")
	}
	for _, address := range addresses {
		if !permittedAddress(address) {
			return nil, errors.New("node DNS resolved a prohibited address")
		}
	}
	dialer := &net.Dialer{Timeout: 8 * time.Second, KeepAlive: 30 * time.Second}
	for _, address := range addresses {
		var connection net.Conn
		connection, err = dialer.DialContext(ctx, network, net.JoinHostPort(address.String(), port))
		if err == nil {
			return connection, nil
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}
	return nil, err
}
func (t *Transport) Close() {
	t.client.CloseIdleConnections()
	t.backup.CloseIdleConnections()
	t.storage.CloseIdleConnections()
}

func (t *Transport) StorageUpload(ctx context.Context, origin, objectID, token string, reader io.Reader, size int64) (map[string]any, error) {
	origin, err := Endpoint(origin)
	if err != nil {
		return nil, err
	}
	if !resourceIdentifier.MatchString(objectID) || len(token) > 4096 || token == "" || size <= 0 {
		return nil, ErrCapability
	}
	req, err := http.NewRequestWithContext(ctx, "PUT", origin+"/internal/v1/storage/"+objectID, reader)
	if err != nil {
		return nil, errors.New("invalid storage upload request")
	}
	req.ContentLength = size
	req.Header.Set("X-Storage-Capability", token)
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("Accept", "application/json")
	resp, err := t.storage.Do(req)
	if err != nil {
		return nil, errors.New("node storage HTTPS upload failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("node storage HTTP %d", resp.StatusCode)
	}
	return readNodeResponse(resp.Body, MaxControlBytes)
}
func (t *Transport) RecordingUpload(ctx context.Context, origin, id, token, contentType string, reader io.Reader, size int64) (map[string]any, error) {
	origin, e := Endpoint(origin)
	if e != nil {
		return nil, e
	}
	if !nodeIdentifier.MatchString(id) || token == "" || len(token) > 4096 || size <= 0 || size > 1024*1024*1024 || !store.RecordingContentType(contentType) {
		return nil, ErrCapability
	}
	req, e := http.NewRequestWithContext(ctx, "PUT", origin+"/internal/v1/recordings/"+id, reader)
	if e != nil {
		return nil, errors.New("invalid recording upload request")
	}
	req.ContentLength = size
	req.Header.Set("X-Recording-Capability", token)
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("Accept-Encoding", "identity")
	req.Header.Set("Accept", "application/json")
	resp, e := t.storage.Do(req)
	if e != nil {
		return nil, errors.New("node recording HTTPS upload failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("node recording HTTP %d", resp.StatusCode)
	}
	return readNodeResponse(resp.Body, 5*1024*1024)
}
func (t *Transport) Request(ctx context.Context, origin, route, method string, value any, relationship, credential string) (map[string]any, error) {
	origin, err := Endpoint(origin)
	if err != nil {
		return nil, err
	}
	u, err := url.Parse(route)
	if err != nil || u.IsAbs() || u.Host != "" || u.Fragment != "" || !strings.HasPrefix(u.Path, "/internal/v1/") || path.Clean(u.Path) != u.Path || strings.Contains(u.Path, "\\") || u.EscapedPath() != u.Path {
		return nil, errors.New("only versioned control endpoints are permitted")
	}
	var body []byte
	if value != nil {
		body, err = protocol.Canonical(value)
		if err != nil {
			return nil, err
		}
	}
	if len(body) > MaxControlBytes {
		return nil, errors.New("control message too large")
	}
	request, err := http.NewRequestWithContext(ctx, method, origin+route, bytes.NewReader(body))
	if err != nil {
		return nil, errors.New("invalid control request")
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Accept-Encoding", "identity")
	if relationship != "" {
		headers, err := protocol.AuthHeaders(credential, relationship, method, route, body, time.Now().Unix())
		if err != nil {
			return nil, err
		}
		for name, value := range headers {
			request.Header.Set(name, value)
		}
	}
	client := t.client
	if strings.HasPrefix(u.Path, "/internal/v1/backup/") {
		client = t.backup
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, errors.New("node control HTTPS request failed")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("node control HTTP %d", response.StatusCode)
	}
	limit := int64(MaxControlBytes)
	if strings.HasPrefix(u.Path, "/internal/v1/recordings/") && strings.HasSuffix(u.Path, "/stat") {
		limit = 5 * 1024 * 1024
	}
	return readNodeResponse(response.Body, limit)
}
func readNodeResponse(body io.Reader, limit int64) (map[string]any, error) {
	raw, err := io.ReadAll(io.LimitReader(body, limit+1))
	if err != nil {
		return nil, errors.New("node control response interrupted")
	}
	if int64(len(raw)) > limit {
		return nil, errors.New("node control response too large")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var result map[string]any
	if decoder.Decode(&result) != nil || result == nil {
		return nil, errors.New("invalid node control response")
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return nil, errors.New("invalid node control response")
	}
	return result, nil
}
func textField(value map[string]any, name string) string {
	result, _ := value[name].(string)
	return result
}
func intField(value map[string]any, name string) (int64, bool) {
	switch value := value[name].(type) {
	case json.Number:
		n, err := value.Int64()
		return n, err == nil
	case int:
		return int64(value), true
	case int64:
		return value, true
	default:
		return 0, false
	}
}
func envelope(value map[string]any) (Envelope, error) {
	payload, ok := value["payload"].(map[string]any)
	signature, signatureOK := value["signature"].(string)
	if !ok || !signatureOK || payload == nil {
		return Envelope{}, errors.New("invalid signed envelope")
	}
	return Envelope{payload, signature}, nil
}
func (t *Transport) Identity(ctx context.Context, origin, expectedID, expectedKey, role string) (Peer, error) {
	challenge, err := randomNodeID()
	if err != nil {
		return Peer{}, err
	}
	raw, err := t.Request(ctx, origin, "/internal/v1/identity?challenge="+challenge, "GET", nil, "", "")
	if err != nil {
		return Peer{}, err
	}
	signed, err := envelope(raw)
	if err != nil {
		return Peer{}, err
	}
	public := textField(signed.Payload, "public_key")
	key := expectedKey
	if key == "" {
		key = public
	}
	if err = protocol.Verify(key, signed.Payload, signed.Signature); err != nil {
		return Peer{}, err
	}
	value := signed.Payload
	version, valid := intField(value, "protocol")
	peer := Peer{textField(value, "node_id"), textField(value, "endpoint"), public, textField(value, "role"), textField(value, "app_version")}
	if textField(value, "challenge") != challenge || !valid || version != protocol.Version || !nodeIdentifier.MatchString(peer.ID) || (expectedID != "" && peer.ID != expectedID) || (expectedKey != "" && public != expectedKey) || (role != "" && peer.Role != role) || (peer.Role != "Standalone" && peer.Role != "Master" && peer.Role != "Follower") || len(peer.AppVersion) > 64 {
		return Peer{}, errors.New("node identity, endpoint or protocol mismatch")
	}
	if peer.Role != "Standalone" {
		endpoint, err := Endpoint(peer.Endpoint)
		expected, expectedErr := Endpoint(origin)
		if err != nil || expectedErr != nil || endpoint != expected {
			return Peer{}, errors.New("node identity endpoint mismatch")
		}
		peer.Endpoint = endpoint
	}
	return peer, nil
}
