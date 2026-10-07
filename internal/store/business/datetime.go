package business

import (
	"fmt"
	"time"
)

// Drivers may expose logical DATETIME as time.Time, bytes or text. Keep this
// physical representation at the persistence boundary, never in a protocol.
type sqlDate struct {
	time.Time
	Valid bool
}

func (v *sqlDate) Scan(value any) error {
	if value == nil {
		v.Valid = false
		return nil
	}
	if t, ok := value.(time.Time); ok {
		v.Time = t.UTC()
		v.Valid = true
		return nil
	}
	var text string
	switch value := value.(type) {
	case string:
		text = value
	case []byte:
		text = string(value)
	default:
		return fmt.Errorf("invalid SQL datetime type %T", value)
	}
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999"} {
		if t, err := time.Parse(layout, text); err == nil {
			v.Time = t.UTC()
			v.Valid = true
			return nil
		}
	}
	return fmt.Errorf("invalid SQL datetime")
}
func isoTime(t time.Time, z bool) string {
	layout := "2006-01-02T15:04:05"
	if t.Nanosecond() != 0 {
		layout += ".000000"
	}
	if z {
		layout += "Z"
	}
	return t.UTC().Format(layout)
}
