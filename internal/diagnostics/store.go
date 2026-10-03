// Package diagnostics implements the deliberately temporary, process-memory-only
// playback probe. Reports are never persisted to SQL, Redis or the filesystem.
package diagnostics

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

const (
	RetireAtISO  = "2026-10-15T00:00:00+00:00"
	TTLSeconds   = 300
	MaxReports   = 24
	MaxBodyBytes = 24 * 1024
)

var retireAt = time.Date(2026, time.October, 15, 0, 0, 0, 0, time.UTC).Unix()

func Enabled(now int64) bool { return now < retireAt }

type field struct {
	key   string
	value any
}
type object []field

// Preserve JSON insertion order: the reference sanitizes the first 64 dictionary
// keys, with duplicate keys replacing values without moving their first slot.
func value(d *json.Decoder, depth int) (any, error) {
	if depth > 128 {
		return nil, errors.New("diagnostic JSON nesting too deep")
	}
	token, err := d.Token()
	if err != nil {
		return nil, err
	}
	switch token {
	case json.Delim('{'):
		fields := object{}
		positions := map[string]int{}
		for d.More() {
			key, err := d.Token()
			if err != nil {
				return nil, err
			}
			name, ok := key.(string)
			if !ok {
				return nil, errors.New("invalid object key")
			}
			v, err := value(d, depth+1)
			if err != nil {
				return nil, err
			}
			if index, found := positions[name]; found {
				fields[index].value = v
			} else {
				positions[name] = len(fields)
				fields = append(fields, field{name, v})
			}
		}
		_, err = d.Token()
		return fields, err
	case json.Delim('['):
		items := []any{}
		for d.More() {
			v, err := value(d, depth+1)
			if err != nil {
				return nil, err
			}
			items = append(items, v)
		}
		_, err = d.Token()
		return items, err
	default:
		return token, nil
	}
}

func bounded(v string, limit int) string {
	if utf8.RuneCountInString(v) <= limit {
		return v
	}
	return string([]rune(v)[:limit])
}
func text(v any, limit int) string {
	switch v := v.(type) {
	case nil:
		return ""
	case string:
		return bounded(v, limit)
	case bool:
		if v {
			return "True"
		}
		return ""
	case json.Number:
		if v == "0" || v == "0.0" {
			return ""
		}
		return bounded(string(v), limit)
	default:
		return bounded(fmt.Sprint(v), limit)
	}
}
func sensitive(key string) bool {
	switch strings.ToLower(key) {
	case "url", "src", "currentsrc", "cookie", "authorization", "token", "headers", "credential", "password", "query", "search", "href":
		return true
	}
	return false
}
func sanitize(v any, depth int) any {
	if depth > 4 {
		return nil
	}
	switch v := v.(type) {
	case string:
		return bounded(v, 512)
	case object:
		clean := map[string]any{}
		for _, f := range v[:min(64, len(v))] {
			key := bounded(f.key, 64)
			if !sensitive(key) {
				clean[key] = sanitize(f.value, depth+1)
			}
		}
		return clean
	case []any:
		clean := []any{}
		for _, item := range v[:min(20, len(v))] {
			clean = append(clean, sanitize(item, depth+1))
		}
		return clean
	case json.Number:
		if strings.ContainsAny(string(v), ".eE") {
			n, err := v.Float64()
			if err != nil || math.IsNaN(n) || math.IsInf(n, 0) {
				return nil
			}
		}
		return v
	default:
		return v
	}
}

func Normalize(raw []byte) (map[string]any, error) {
	if len(raw) > MaxBodyBytes {
		return nil, errors.New("diagnostic report too large")
	}
	if len(raw) == 0 {
		raw = []byte("{}")
	}
	if !utf8.Valid(raw) {
		return nil, errors.New("invalid UTF-8 diagnostic report")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	v, err := value(d, 0)
	if err != nil {
		return nil, err
	}
	if _, err = d.Token(); err != io.EOF {
		return nil, errors.New("trailing diagnostic JSON")
	}
	fields, ok := v.(object)
	if !ok {
		return nil, errors.New("diagnostic report is not an object")
	}
	clean := map[string]any{}
	for _, f := range fields {
		switch f.key {
		case "diagnostic_id", "stage", "reason", "sent_at_ms", "client", "track", "sample", "timeline":
			clean[f.key] = sanitize(f.value, 0)
		}
	}
	clean["diagnostic_id"], clean["stage"], clean["reason"] = text(clean["diagnostic_id"], 96), text(clean["stage"], 96), text(clean["reason"], 256)
	if _, ok := clean["timeline"].([]any); !ok {
		clean["timeline"] = []any{}
	}
	return clean, nil
}

type report struct {
	received int64
	raw      []byte
}
type Store struct {
	mu      sync.Mutex
	reports []report
}

func New() *Store { return &Store{} }
func encode(v any) ([]byte, error) {
	var b bytes.Buffer
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	err := e.Encode(v)
	return bytes.TrimSuffix(b.Bytes(), []byte("\n")), err
}
func (s *Store) purge(now int64) {
	kept := s.reports[:0]
	for _, v := range s.reports {
		if v.received >= now-TTLSeconds {
			kept = append(kept, v)
		}
	}
	// Clear references to expired payloads, not just the slice length.
	clear(s.reports[len(kept):])
	s.reports = kept
}
func (s *Store) Add(payload map[string]any, source, delivery string, now int64) error {
	// Marshal/copy before storing: callers and snapshots cannot mutate retained
	// reports or introduce unbounded keys after normalization.
	b, err := encode(payload)
	if err != nil {
		return err
	}
	clean, err := Normalize(b)
	if err != nil {
		return err
	}
	clean["received_at"], clean["expires_at"] = now, now+TTLSeconds
	clean["source_node"], clean["delivery"] = bounded(source, 64), bounded(delivery, 32)
	b, err = encode(clean)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.purge(now)
	if len(s.reports) == MaxReports {
		copy(s.reports, s.reports[1:])
		s.reports = s.reports[:MaxReports-1]
	}
	s.reports = append(s.reports, report{now, b})
	return nil
}
func (s *Store) Snapshot(now int64) map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.purge(now)
	reports := []map[string]any{}
	for _, v := range s.reports {
		var p map[string]any
		d := json.NewDecoder(bytes.NewReader(v.raw))
		d.UseNumber()
		_ = d.Decode(&p)
		reports = append(reports, p)
	}
	return map[string]any{"enabled": Enabled(now), "generated_at": now, "ttl_seconds": TTLSeconds, "max_reports": MaxReports, "retire_at": RetireAtISO, "reports": reports}
}
func (s *Store) Clear() { s.mu.Lock(); defer s.mu.Unlock(); clear(s.reports); s.reports = nil }
