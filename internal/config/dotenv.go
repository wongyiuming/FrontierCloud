package config

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
)

var envKey = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// readDotEnv does not mutate the process environment. Explicit environment
// variables have precedence, including an explicitly empty value.
func readDotEnv(path string) (map[string]string, error) {
	values := map[string]string{}
	f, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return values, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	for line := 1; scanner.Scan(); line++ {
		text := strings.TrimSpace(strings.TrimPrefix(scanner.Text(), "\ufeff"))
		if text == "" || strings.HasPrefix(text, "#") {
			continue
		}
		text = strings.TrimPrefix(text, "export ")
		key, value, ok := strings.Cut(text, "=")
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		if !ok || !envKey.MatchString(key) {
			return nil, fmt.Errorf("invalid .env syntax on line %d", line)
		}
		if strings.HasPrefix(value, "\"") || strings.HasPrefix(value, "'") {
			quote := value[0]
			end := -1
			for i := 1; i < len(value); i++ {
				if value[i] == '\\' && quote == '"' {
					i++
					continue
				}
				if value[i] == quote {
					end = i
					break
				}
			}
			if end < 0 {
				return nil, fmt.Errorf("unterminated .env quote on line %d", line)
			}
			tail := strings.TrimSpace(value[end+1:])
			if tail != "" && !strings.HasPrefix(tail, "#") {
				return nil, fmt.Errorf("invalid .env quote suffix on line %d", line)
			}
			if quote == '"' {
				value, err = strconv.Unquote(value[:end+1])
				if err != nil {
					return nil, fmt.Errorf("invalid .env escape on line %d", line)
				}
			} else {
				value = value[1:end]
			}
		} else {
			if i := strings.Index(value, " #"); i >= 0 {
				value = strings.TrimSpace(value[:i])
			}
		}
		values[key] = value
	}
	return values, scanner.Err()
}
