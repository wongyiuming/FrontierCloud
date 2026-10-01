// Package network resolves client identity only from configured trusted peers.
package network

import (
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"strings"
)

type Resolver struct{ networks []netip.Prefix }
type Identity struct {
	IP                   string
	PeerIP               string
	FromTrustedProxy     bool
	TrustedHeaderMissing bool
}

func New(networks []string) (*Resolver, error) {
	r := &Resolver{}
	for _, raw := range networks {
		raw = strings.TrimSpace(raw)
		if raw == "" {
			continue
		}
		prefix, err := netip.ParsePrefix(raw)
		if err != nil {
			return nil, fmt.Errorf("invalid TRUSTED_PROXY_NETWORKS: %w", err)
		}
		r.networks = append(r.networks, prefix.Masked())
	}
	return r, nil
}
func canonical(raw string) string {
	address, err := netip.ParseAddr(strings.TrimSpace(raw))
	if err != nil || address.Zone() != "" {
		return ""
	}
	if address.Is4In6() {
		bytes := address.As16()
		return fmt.Sprintf("::ffff:%x:%x", uint16(bytes[12])<<8|uint16(bytes[13]), uint16(bytes[14])<<8|uint16(bytes[15]))
	}
	return address.String()
}

func Normalize(raw string) (string, error) {
	value := canonical(raw)
	if value == "" {
		return "", fmt.Errorf("IP 地址无效")
	}
	return value, nil
}
func (r *Resolver) Resolve(request *http.Request) Identity {
	peer, _, err := net.SplitHostPort(request.RemoteAddr)
	if err != nil {
		peer = request.RemoteAddr
	}
	peer = canonical(peer)
	if peer == "" {
		peer = "0.0.0.0"
	}
	result := Identity{IP: peer, PeerIP: peer}
	address, _ := netip.ParseAddr(peer)
	trusted := false
	for _, prefix := range r.networks {
		if prefix.Contains(address) {
			trusted = true
			break
		}
	}
	if !trusted {
		return result
	}
	values := request.Header.Values("X-Real-IP")
	real := ""
	if len(values) == 1 && !strings.Contains(values[0], ",") {
		real = canonical(values[0])
	}
	if real == "" {
		result.TrustedHeaderMissing = true
		return result
	}
	result.IP = real
	result.FromTrustedProxy = true
	return result
}
func (r *Resolver) SecureAdmin(request *http.Request) bool {
	identity := r.Resolve(request)
	if request.TLS != nil {
		return true
	}
	if identity.FromTrustedProxy {
		return strings.ToLower(strings.TrimSpace(request.Header.Get("X-Forwarded-Proto"))) == "https" && len(request.Header.Values("X-Forwarded-Proto")) == 1
	}
	if identity.TrustedHeaderMissing || (identity.PeerIP != "127.0.0.1" && identity.PeerIP != "::1") {
		return false
	}
	for _, name := range []string{"Forwarded", "X-Forwarded-For", "X-Forwarded-Proto", "X-Real-IP"} {
		if len(request.Header.Values(name)) > 0 {
			return false
		}
	}
	return true
}
