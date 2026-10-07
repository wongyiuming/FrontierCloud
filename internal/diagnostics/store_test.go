package diagnostics

import (
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"testing"
)

func TestNormalizationMatchesReferenceBoundsAndPrivacy(t *testing.T) {
	items := []string{}
	for i := 0; i < 25; i++ {
		items = append(items, fmt.Sprintf(`{"event":%d,"href":"private"}`, i))
	}
	raw := `{"diagnostic_id":"` + strings.Repeat("界", 110) + `","stage":true,"reason":"` + strings.Repeat("啊", 300) + `","unknown":"secret","sample":{"currentSrc":"secret","AUTHORIZATION":"secret","paused":true,"safe":{"password":"secret","value":"ok"},"huge":1e999},"timeline":[` + strings.Join(items, ",") + `]}`
	p, err := Normalize([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	if len([]rune(p["diagnostic_id"].(string))) != 96 || p["stage"] != "True" || len([]rune(p["reason"].(string))) != 256 || len(p["timeline"].([]any)) != 20 {
		t.Fatal(p)
	}
	b, _ := json.Marshal(p)
	if strings.Contains(string(b), "secret") || strings.Contains(string(b), "href") || p["sample"].(map[string]any)["huge"] != nil {
		t.Fatal(string(b))
	}
	if _, ok := p["unknown"]; ok {
		t.Fatal("unknown top level retained")
	}
	for _, raw := range []string{`[]`, `null`, `{} {}`, `{"x":`, string([]byte{0xff})} {
		if _, err := Normalize([]byte(raw)); err == nil {
			t.Fatal("malformed accepted", raw)
		}
	}
	if _, err := Normalize([]byte(strings.Repeat(" ", MaxBodyBytes+1))); err == nil {
		t.Fatal("oversize accepted")
	}
}
func TestNormalizationPreservesFirstDictionarySlotsAndDepth(t *testing.T) {
	fields := []string{`"first":1`, `"first":2`}
	for i := 0; i < 64; i++ {
		fields = append(fields, fmt.Sprintf(`"k%d":%d`, i, i))
	}
	p, err := Normalize([]byte(`{"sample":{` + strings.Join(fields, ",") + `},"client":{"a":{"b":{"c":{"d":{"e":"too deep"}}}}}}`))
	if err != nil {
		t.Fatal(err)
	}
	sample := p["sample"].(map[string]any)
	if len(sample) != 64 || sample["first"] != json.Number("2") || sample["k62"] != json.Number("62") {
		t.Fatal(sample)
	}
	if _, ok := sample["k63"]; ok {
		t.Fatal("later input key retained")
	}
	v := p["client"].(map[string]any)["a"].(map[string]any)["b"].(map[string]any)["c"].(map[string]any)["d"].(map[string]any)
	if v["e"] != nil {
		t.Fatal("nesting exceeded reference depth", v)
	}
}
func TestEphemeralStoreBoundTTLIsolationAndConcurrentClear(t *testing.T) {
	s := New()
	now := int64(1790800000)
	for i := 0; i < MaxReports+5; i++ {
		if err := s.Add(map[string]any{"diagnostic_id": fmt.Sprint(i), "stage": "sample"}, "node", "test", now); err != nil {
			t.Fatal(err)
		}
	}
	reports := s.Snapshot(now)["reports"].([]map[string]any)
	if len(reports) != MaxReports || reports[0]["diagnostic_id"] != "5" {
		t.Fatal(reports)
	}
	reports[0]["diagnostic_id"] = "mutated"
	if s.Snapshot(now)["reports"].([]map[string]any)[0]["diagnostic_id"] != "5" {
		t.Fatal("snapshot modified retained reports")
	}
	if len(s.Snapshot(now + TTLSeconds)["reports"].([]map[string]any)) != MaxReports || len(s.Snapshot(now + TTLSeconds + 1)["reports"].([]map[string]any)) != 0 {
		t.Fatal("TTL boundary")
	}
	if len(New().Snapshot(now)["reports"].([]map[string]any)) != 0 {
		t.Fatal("reports survived process-store replacement")
	}
	if Enabled(retireAt) || !Enabled(retireAt-1) {
		t.Fatal("retirement boundary")
	}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 20; j++ {
				if err := s.Add(map[string]any{"diagnostic_id": "a", "stage": "sample"}, "node", "test", now); err != nil {
					t.Error(err)
				}
				s.Snapshot(now)
				if j%3 == 0 {
					s.Clear()
				}
			}
		}()
	}
	wg.Wait()
	s.Clear()
	if len(s.Snapshot(now)["reports"].([]map[string]any)) != 0 {
		t.Fatal("clear failed")
	}
}
func TestAcceptedHTMLCharactersDoNotOverflowInternalEncoding(t *testing.T) {
	fields := []string{}
	for i := 0; i < 35; i++ {
		fields = append(fields, fmt.Sprintf(`"k%d":"%s"`, i, strings.Repeat("<", 512)))
	}
	p, err := Normalize([]byte(`{"diagnostic_id":"a","stage":"sample","sample":{` + strings.Join(fields, ",") + `}}`))
	if err != nil {
		t.Fatal(err)
	}
	if err = New().Add(p, "node", "test", 1790800000); err != nil {
		t.Fatal("accepted bounded report cannot be retained", err)
	}
}
