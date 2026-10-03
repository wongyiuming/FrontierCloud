package protocol

import (
	"errors"
	"regexp"
	"slices"
)

var capabilityName = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

// BaselineCapabilities names only the established protocol-v2 operations.
// It does not advertise unfinished restore, release-manifest or worker features.
func BaselineCapabilities() []string {
	return []string{"backup-v2", "media-v2", "node-auth-v2", "recordings-v2", "storage-v2"}
}

// ReadCapabilities is called only after authenticating a protocol-v2 peer.
// Older peers lacking the optional field retain the documented v2 baseline;
// explicit empty lists mean no optional operation and never imply new features.
func ReadCapabilities(message map[string]any) ([]string, error) {
	v, present := message["capabilities"]
	if !present {
		return BaselineCapabilities(), nil
	}
	var values []any
	switch v := v.(type) {
	case []any:
		values = v
	case []string:
		for _, item := range v {
			values = append(values, item)
		}
	default:
		return nil, errors.New("invalid capability list")
	}
	if len(values) > 64 {
		return nil, errors.New("capability list exceeds limit")
	}
	result := []string{}
	seen := map[string]bool{}
	for _, v := range values {
		name, ok := v.(string)
		if !ok || !capabilityName.MatchString(name) || seen[name] {
			return nil, errors.New("invalid or duplicate capability")
		}
		seen[name] = true
		result = append(result, name)
	}
	slices.Sort(result)
	return result, nil
}

func NegotiateCapabilities(message map[string]any, required ...string) ([]string, error) {
	peer, err := ReadCapabilities(message)
	if err != nil {
		return nil, err
	}
	selected := []string{}
	for _, local := range BaselineCapabilities() {
		if slices.Contains(peer, local) {
			selected = append(selected, local)
		}
	}
	for _, feature := range required {
		if !slices.Contains(selected, feature) {
			return nil, errors.New("required protocol capability unavailable")
		}
	}
	return selected, nil
}
