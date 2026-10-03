// Package contracts embeds language-neutral protocol artifacts. No runtime
// generation step imports the reference Python implementation.
package contracts

import _ "embed"

// OpenAPI is the reviewed effective public API schema shared by both runtimes.
//
//go:embed v2/openapi.json
var OpenAPI []byte
