package protocol

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"unicode/utf8"
)

// ParseStrictJSON preserves integer values and rejects ambiguity before typed
// interpretation. The caller supplies its own message/artifact byte boundary.
func ParseStrictJSON(raw []byte, maximum int) (any, error) {
	if maximum <= 0 || len(raw) > maximum || !utf8.Valid(raw) {
		return nil, errors.New("invalid bounded JSON")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	value, err := strictValue(d, 0)
	if err != nil {
		return nil, err
	}
	if _, err = d.Token(); err != io.EOF {
		return nil, errors.New("trailing JSON")
	}
	return value, nil
}
func strictValue(d *json.Decoder, depth int) (any, error) {
	if depth > 32 {
		return nil, errors.New("JSON depth exceeds 32")
	}
	token, err := d.Token()
	if err != nil {
		return nil, err
	}
	delim, ok := token.(json.Delim)
	if !ok {
		return token, nil
	}
	switch delim {
	case '{':
		value := map[string]any{}
		for d.More() {
			key, err := d.Token()
			if err != nil {
				return nil, err
			}
			name, ok := key.(string)
			if !ok {
				return nil, errors.New("invalid JSON key")
			}
			if _, ok := value[name]; ok {
				return nil, errors.New("duplicate JSON key")
			}
			child, err := strictValue(d, depth+1)
			if err != nil {
				return nil, err
			}
			value[name] = child
		}
		end, err := d.Token()
		if err != nil || end != json.Delim('}') {
			return nil, errors.New("invalid JSON object")
		}
		return value, nil
	case '[':
		value := []any{}
		for d.More() {
			child, err := strictValue(d, depth+1)
			if err != nil {
				return nil, err
			}
			value = append(value, child)
		}
		end, err := d.Token()
		if err != nil || end != json.Delim(']') {
			return nil, errors.New("invalid JSON array")
		}
		return value, nil
	}
	return nil, errors.New("unexpected JSON delimiter")
}
