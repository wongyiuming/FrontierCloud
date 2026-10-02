package protocol

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestStrictJSONPreservesIntegersAndRejectsAmbiguity(t *testing.T) {
	for _, raw := range []string{`{"a":1,"a":2}`, `{"a":{"v":1,"v":2}}`, `{} {}`, `{"a":1,}`, string([]byte{'"', 255, '"'}), strings.Repeat("[", 34) + "0" + strings.Repeat("]", 34)} {
		if _, err := ParseStrictJSON([]byte(raw), 1024); err == nil {
			t.Fatal("ambiguous JSON accepted", raw)
		}
	}
	value, err := ParseStrictJSON([]byte(`{"值":[9007199254740993,1.0,null]}`), 1024)
	if err != nil {
		t.Fatal(err)
	}
	array := value.(map[string]any)["值"].([]any)
	if array[0] != (json.Number("9007199254740993")) || array[1] != (json.Number("1.0")) {
		t.Fatal("JSON numeric identity lost", array)
	}
	if _, err = ParseStrictJSON([]byte(`{}`), 1); err == nil {
		t.Fatal("byte boundary ignored")
	}
}
