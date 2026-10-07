package main

import (
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"testing"
)

func TestHealthAddressUsesConfiguredListener(t *testing.T) {
	for input, want := range map[string]string{":9000": "127.0.0.1:9000", "0.0.0.0:8010": "127.0.0.1:8010", "[::]:9001": "[::1]:9001", "127.0.0.2:8080": "127.0.0.2:8080"} {
		got, err := healthAddress(input)
		if err != nil || got != want {
			t.Fatalf("%s: %s %v", input, got, err)
		}
	}
	if _, err := healthAddress("not-an-address"); err == nil {
		t.Fatal("invalid listener accepted")
	}
}

func TestDurableNativeRolesCannotRestartWithDowngradedTransport(t *testing.T) {
	for _, role := range []string{"Master", "Follower"} {
		identity := &node.Identity{NodeIdentity: store.NodeIdentity{Role: role, Endpoint: "https://site.fleet.invalid"}}
		if err := runtimeIdentityTransport(config.Config{}, identity); err == nil || identity.Role != role {
			t.Fatal("fixed role admitted cleartext or was reset", role, err)
		}
		if err := runtimeIdentityTransport(config.Config{TLSEnabled: true}, identity); err != nil {
			t.Fatal(err)
		}
		for _, endpoint := range []string{"", "http://site.fleet.invalid", "https://site.fleet.invalid/path"} {
			identity.Endpoint = endpoint
			if err := runtimeIdentityTransport(config.Config{TLSEnabled: true}, identity); err == nil {
				t.Fatal("invalid durable endpoint admitted", role)
			}
		}
	}
	if err := runtimeIdentityTransport(config.Config{}, &node.Identity{NodeIdentity: store.NodeIdentity{Role: "Standalone"}}); err != nil {
		t.Fatal("HTTP Standalone blocked", err)
	}
}
