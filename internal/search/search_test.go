package search

import (
	"encoding/json"
	"os"
	"strings"
	"sync"
	"testing"
)

func TestPythonSearchConformance(t *testing.T) {
	data, err := os.ReadFile("../../protocol/v2/vectors/search.json")
	if err != nil {
		t.Fatal(err)
	}
	var vectors struct {
		Cases []struct{ Value, Compact, Pinyin, Aliases string }
	}
	if err := json.Unmarshal(data, &vectors); err != nil {
		t.Fatal(err)
	}
	e, err := New()
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range vectors.Cases {
		t.Run(test.Value, func(t *testing.T) {
			if got := Compact(test.Value); got != test.Compact {
				t.Errorf("compact: %q != %q", got, test.Compact)
			}
			if got := e.Pinyin(test.Value); got != test.Pinyin {
				t.Errorf("pinyin: %q != %q", got, test.Pinyin)
			}
			text, err := e.Text(test.Value)
			if err != nil || text != test.Aliases {
				t.Errorf("aliases: %q != %q (%v)", text, test.Aliases, err)
			}
		})
	}
}

func TestSearchQueryLimitsAndConcurrentAliases(t *testing.T) {
	for _, value := range []string{"", " ", "#_+-", strings.Repeat("字", 101), string([]byte{0xff})} {
		if _, err := Query(value); err == nil {
			t.Errorf("invalid query accepted: %.20q", value)
		}
	}
	if query, err := Query("  ＡｎＹｏｎＧ　 "); err != nil || query != "anyong" {
		t.Fatalf("query: %q %v", query, err)
	}
	e, err := New()
	if err != nil {
		t.Fatal(err)
	}
	var workers sync.WaitGroup
	for range 16 {
		workers.Go(func() {
			for range 25 {
				text, err := e.Text("music/黃耀明/暗湧.mp3")
				if err != nil || !strings.Contains(text, "anyong") || !strings.Contains(text, "暗涌") {
					t.Errorf("concurrent aliases: %s %v", text, err)
					return
				}
			}
		})
	}
	workers.Wait()
}
