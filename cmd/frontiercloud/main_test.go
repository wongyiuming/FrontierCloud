package main

import "testing"

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
