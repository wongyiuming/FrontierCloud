package network

import (
	"crypto/tls"
	"net/http/httptest"
	"testing"
)

func TestAdminTransportAndClientIdentity(t *testing.T) {
	resolver, err := New([]string{"172.16.0.0/12"})
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, peer, real, proto      string
		duplicate, tls, secure, from bool
		ip                           string
	}{
		{name: "untrusted spoof", peer: "192.0.2.1:123", real: "127.0.0.1", proto: "https", ip: "192.0.2.1"},
		{name: "direct loopback", peer: "127.0.0.1:123", secure: true, ip: "127.0.0.1"},
		{name: "loopback proxy header", peer: "127.0.0.1:123", proto: "https", ip: "127.0.0.1"},
		{name: "trusted HTTPS", peer: "172.20.0.2:123", real: "2001:db8::1", proto: "https", secure: true, from: true, ip: "2001:db8::1"},
		{name: "trusted HTTP", peer: "172.20.0.2:123", real: "192.0.2.1", proto: "http", from: true, ip: "192.0.2.1"},
		{name: "trusted missing verified IP", peer: "172.20.0.2:123", proto: "https", ip: "172.20.0.2"},
		{name: "duplicate verified IP", peer: "172.20.0.2:123", real: "192.0.2.1", proto: "https", duplicate: true, ip: "172.20.0.2"},
		{name: "direct TLS", peer: "192.0.2.1:123", tls: true, secure: true, ip: "192.0.2.1"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			request := httptest.NewRequest("GET", "http://example.com/admin", nil)
			request.RemoteAddr = tc.peer
			if tc.real != "" {
				request.Header.Set("X-Real-IP", tc.real)
			}
			if tc.proto != "" {
				request.Header.Set("X-Forwarded-Proto", tc.proto)
			}
			if tc.duplicate {
				request.Header.Add("X-Real-IP", tc.real)
			}
			if tc.tls {
				request.TLS = &tls.ConnectionState{}
			}
			id := resolver.Resolve(request)
			if id.IP != tc.ip || id.FromTrustedProxy != tc.from || resolver.SecureAdmin(request) != tc.secure {
				t.Fatalf("identity=%+v secure=%v", id, resolver.SecureAdmin(request))
			}
		})
	}
}

func TestIPNormalizationMatchesReference(t *testing.T) {
	for raw, want := range map[string]string{" 2001:0DB8:0:0::1 ": "2001:db8::1", "::ffff:192.168.1.1": "::ffff:c0a8:101", "192.0.2.1": "192.0.2.1"} {
		actual, err := Normalize(raw)
		if err != nil || actual != want {
			t.Fatalf("canonical IP %q: %s %v", raw, actual, err)
		}
	}
	for _, raw := range []string{"192.168.001.1", "fe80::1%eth0", "127.0.0.1,192.0.2.1", ""} {
		if _, err := Normalize(raw); err == nil {
			t.Fatalf("invalid IP accepted %q", raw)
		}
	}
}
