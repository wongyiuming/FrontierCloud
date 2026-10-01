package recording

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestRecordingReceiptLegacyIdentityAndStrictMetadata(t *testing.T) {
	id := strings.Repeat("a", 32)
	base := map[string]any{"size_bytes": json.Number("12"), "sha256": strings.Repeat("b", 64), "metadata": map[string]any{}}
	v, e := Receipt(base, id, 12)
	if e != nil || v.ID != id {
		t.Fatal("legacy Python receipt", v, e)
	}
	for _, bad := range []any{nil, true, "1", json.Number("-1"), json.Number("1e999999")} {
		base["metadata"] = map[string]any{"lyrics": []any{map[string]any{"time": bad, "text": "line"}}}
		if _, e = Receipt(base, id, 12); e == nil {
			t.Fatal("invalid timeline", bad)
		}
	}
	base["metadata"] = map[string]any{"lyrics": []any{map[string]any{"time": json.Number("1.25"), "text": "歌词"}}}
	if _, e = Receipt(base, id, 12); e != nil {
		t.Fatal(e)
	}
	if _, e = Receipt(base, id, 11); e == nil {
		t.Fatal("oversized receipt")
	}
	base["recording_id"] = strings.Repeat("c", 32)
	if _, e = Receipt(base, id, 12); e == nil {
		t.Fatal("foreign recording receipt")
	}
}
