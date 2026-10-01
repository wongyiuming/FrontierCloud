package protocol

import (
	"bytes"
	"math"
	"strings"
	"testing"
)

func TestOutboundAuthBindsFullRequestAndRejectsOverflowStamp(t *testing.T) {
	credential := Encode(bytes.Repeat([]byte{1}, 48))
	relationship := strings.Repeat("a", 32)
	body := []byte(`{"value":"test"}`)
	headers, err := AuthHeaders(credential, relationship, "POST", "/internal/v1/heartbeat?scope=one", body, 1000)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = VerifyAuth(credential, headers, "POST", "/internal/v1/heartbeat?scope=one", body, 1000); err != nil {
		t.Fatal(err)
	}
	for _, change := range []struct {
		method, path string
		body         []byte
	}{{"GET", "/internal/v1/heartbeat?scope=one", body}, {"POST", "/internal/v1/heartbeat?scope=two", body}, {"POST", "/internal/v1/heartbeat?scope=one", []byte("changed")}} {
		if _, err = VerifyAuth(credential, headers, change.method, change.path, change.body, 1000); err == nil {
			t.Fatal("request mutation authenticated")
		}
	}
	if _, err = VerifyAuth(credential, headers, "POST", "/internal/v1/heartbeat?scope=one", body, 1060); err != nil {
		t.Fatal("inclusive skew", err)
	}
	if _, err = VerifyAuth(credential, headers, "POST", "/internal/v1/heartbeat?scope=one", body, 1061); err == nil {
		t.Fatal("expired auth accepted")
	}
	for _, stamp := range []int64{math.MinInt64 + 1000, math.MaxInt64} {
		headers, err = AuthHeaders(credential, relationship, "POST", "/internal/v1/heartbeat", body, stamp)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = VerifyAuth(credential, headers, "POST", "/internal/v1/heartbeat", body, 1000); err == nil {
			t.Fatal("overflow timestamp accepted")
		}
	}
}
