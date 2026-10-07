package protocol

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

func TestSharedCapabilityNegotiationVectors(t *testing.T) {
	b, err := os.ReadFile("../../protocol/v2/vectors/capabilities.json")
	if err != nil {
		t.Fatal(err)
	}
	var vectors struct {
		Cases []struct {
			Name               string
			Message            map[string]any
			Valid, Compatible  bool
			Required, Selected []string
		}
	}
	if err = json.Unmarshal(b, &vectors); err != nil {
		t.Fatal(err)
	}
	for _, v := range vectors.Cases {
		t.Run(v.Name, func(t *testing.T) {
			_, err := ReadCapabilities(v.Message)
			if (err == nil) != v.Valid {
				t.Fatal("validation mismatch", err)
			}
			if !v.Valid {
				return
			}
			selected, err := NegotiateCapabilities(v.Message)
			if err != nil || !reflect.DeepEqual(selected, v.Selected) {
				t.Fatal(selected, err)
			}
			_, err = NegotiateCapabilities(v.Message, v.Required...)
			if (err == nil) != v.Compatible {
				t.Fatal("required operation not negotiated", err)
			}
		})
	}
	values := make([]string, 65)
	for i := range values {
		values[i] = "valid-name"
	}
	if _, err = ReadCapabilities(map[string]any{"capabilities": values}); err == nil {
		t.Fatal("unbounded list admitted")
	}
	baseline := BaselineCapabilities()
	baseline[0] = "mutated"
	if BaselineCapabilities()[0] == "mutated" {
		t.Fatal("caller modified local advertisement")
	}
}
