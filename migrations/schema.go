// Package migrations owns the physical schemas used by both runtime implementations.
package migrations

import (
	"embed"
	"encoding/json"
	"fmt"
)

const Generation = 2
const JournalName = "create-schema-migration-journal"
const JournalChecksum = "aee46be69d8c5b005cbee58ebfd37feb41d3431eb1fb23bb8339c9076147c03e"

//go:embed mysql/*.json sqlite/*.json
var assets embed.FS

func Statements(backend string) ([]string, error) {
	if backend != "mysql" && backend != "sqlite" {
		return nil, fmt.Errorf("unsupported schema backend %q", backend)
	}
	data, err := assets.ReadFile(fmt.Sprintf("%s/%04d-schema.json", backend, Generation))
	if err != nil {
		return nil, err
	}
	var document struct {
		Generation int      `json:"generation"`
		Statements []string `json:"statements"`
	}
	if err := json.Unmarshal(data, &document); err != nil {
		return nil, err
	}
	if document.Generation != Generation {
		return nil, fmt.Errorf("shared schema generation mismatch")
	}
	return document.Statements, nil
}
