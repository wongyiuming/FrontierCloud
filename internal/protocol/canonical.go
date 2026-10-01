package protocol

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"math/big"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

var jsonNumberPattern = regexp.MustCompile(`^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$`)

// Canonical preserves the protocol's JSON types and exact UTF-8 signing bytes.
// Decode wire JSON with UseNumber: float64 cannot retain large integer identities
// or distinguish an integer 1 from a floating-point 1.0.
func Canonical(value any) ([]byte, error) {
	var output bytes.Buffer
	if err := appendCanonical(&output, reflect.ValueOf(value), 0); err != nil {
		return nil, err
	}
	return output.Bytes(), nil
}

func appendCanonical(output *bytes.Buffer, value reflect.Value, depth int) error {
	if depth > 2048 {
		return fmt.Errorf("canonical JSON nesting exceeds 2048")
	}
	if !value.IsValid() {
		output.WriteString("null")
		return nil
	}
	if value.Kind() == reflect.Interface || value.Kind() == reflect.Pointer {
		if value.IsNil() {
			output.WriteString("null")
			return nil
		}
		return appendCanonical(output, value.Elem(), depth+1)
	}
	if (value.Kind() == reflect.Map || value.Kind() == reflect.Slice) && value.IsNil() {
		output.WriteString("null")
		return nil
	}
	if value.Type() == reflect.TypeFor[json.Number]() {
		number := value.Interface().(json.Number).String()
		if !jsonNumberPattern.MatchString(number) {
			return fmt.Errorf("invalid JSON number")
		}
		if strings.ContainsAny(number, ".eE") {
			parsed, err := strconv.ParseFloat(number, 64)
			if err != nil {
				return err
			}
			return appendFloat(output, parsed)
		}
		integer, ok := new(big.Int).SetString(number, 10)
		if !ok {
			return fmt.Errorf("invalid JSON integer")
		}
		output.WriteString(integer.String())
		return nil
	}
	switch value.Kind() {
	case reflect.Bool:
		output.WriteString(strconv.FormatBool(value.Bool()))
	case reflect.String:
		return appendString(output, value.String())
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		output.WriteString(strconv.FormatInt(value.Int(), 10))
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		output.WriteString(strconv.FormatUint(value.Uint(), 10))
	case reflect.Float32, reflect.Float64:
		return appendFloat(output, value.Float())
	case reflect.Map:
		if value.Type().Key().Kind() != reflect.String {
			return fmt.Errorf("canonical JSON object keys must be strings")
		}
		keys := value.MapKeys()
		sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
		output.WriteByte('{')
		for index, key := range keys {
			if index > 0 {
				output.WriteByte(',')
			}
			if err := appendString(output, key.String()); err != nil {
				return err
			}
			output.WriteByte(':')
			if err := appendCanonical(output, value.MapIndex(key), depth+1); err != nil {
				return err
			}
		}
		output.WriteByte('}')
	case reflect.Slice, reflect.Array:
		output.WriteByte('[')
		for index := 0; index < value.Len(); index++ {
			if index > 0 {
				output.WriteByte(',')
			}
			if err := appendCanonical(output, value.Index(index), depth+1); err != nil {
				return err
			}
		}
		output.WriteByte(']')
	default:
		return fmt.Errorf("unsupported canonical JSON type %s", value.Type())
	}
	return nil
}

func appendFloat(output *bytes.Buffer, number float64) error {
	if math.IsNaN(number) || math.IsInf(number, 0) {
		return fmt.Errorf("canonical JSON rejects non-finite numbers")
	}
	// Python's shortest binary64 representation uses fixed notation for decimal
	// exponents -4 through 15 and retains .0 for integral floating-point values.
	scientific := strconv.FormatFloat(number, 'e', -1, 64)
	exponent, err := strconv.Atoi(scientific[strings.LastIndexByte(scientific, 'e')+1:])
	if err != nil {
		return err
	}
	if exponent >= -4 && exponent < 16 {
		fixed := strconv.FormatFloat(number, 'f', -1, 64)
		if !strings.ContainsRune(fixed, '.') {
			fixed += ".0"
		}
		output.WriteString(fixed)
	} else {
		output.WriteString(scientific)
	}
	return nil
}

func appendString(output *bytes.Buffer, value string) error {
	if !utf8.ValidString(value) {
		return fmt.Errorf("canonical JSON string is not valid UTF-8")
	}
	output.WriteByte('"')
	for _, character := range value {
		switch character {
		case '"', '\\':
			output.WriteByte('\\')
			output.WriteRune(character)
		case '\b':
			output.WriteString(`\b`)
		case '\f':
			output.WriteString(`\f`)
		case '\n':
			output.WriteString(`\n`)
		case '\r':
			output.WriteString(`\r`)
		case '\t':
			output.WriteString(`\t`)
		default:
			if character < 0x20 {
				fmt.Fprintf(output, `\u%04x`, character)
			} else {
				output.WriteRune(character)
			}
		}
	}
	output.WriteByte('"')
	return nil
}
