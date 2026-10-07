// Package search preserves the existing Unicode, OpenCC and phrase-aware
// pronunciation aliases using embedded data and native Go only.
package search

import (
	"bytes"
	"compress/gzip"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"sync"
	"unicode"
	"unicode/utf8"

	"github.com/longbridgeapp/opencc"
	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
)

//go:embed data/pinyin.json.gz
var pronunciations []byte

var ErrQuery = errors.New("invalid search query")

type trie struct {
	Next  map[rune]*trie
	Value string
}
type Engine struct {
	simplified, traditional *opencc.OpenCC
	characters              map[rune]string
	phrases                 *trie
}

var defaultEngine = sync.OnceValues(func() (*Engine, error) {
	z, err := gzip.NewReader(bytes.NewReader(pronunciations))
	if err != nil {
		return nil, err
	}
	defer z.Close()
	data, err := io.ReadAll(io.LimitReader(z, 16*1024*1024))
	if err != nil {
		return nil, err
	}
	var dictionary struct {
		Characters map[string]string `json:"characters"`
		Phrases    map[string]string `json:"phrases"`
	}
	if err := json.Unmarshal(data, &dictionary); err != nil {
		return nil, err
	}
	if len(dictionary.Characters) != 41923 || len(dictionary.Phrases) != 47111 {
		return nil, errors.New("invalid embedded pronunciation data")
	}
	e := &Engine{characters: map[rune]string{}, phrases: &trie{Next: map[rune]*trie{}}}
	for word, value := range dictionary.Characters {
		r, size := utf8.DecodeRuneInString(word)
		if size != len(word) {
			return nil, errors.New("invalid character pronunciation")
		}
		e.characters[r] = value
	}
	for word, value := range dictionary.Phrases {
		current := e.phrases
		for _, r := range word {
			if current.Next[r] == nil {
				current.Next[r] = &trie{Next: map[rune]*trie{}}
			}
			current = current.Next[r]
		}
		current.Value = value
	}
	e.simplified, err = opencc.New("t2s")
	if err != nil {
		return nil, err
	}
	e.traditional, err = opencc.New("s2t")
	if err != nil {
		return nil, err
	}
	return e, nil
})

func New() (*Engine, error) { return defaultEngine() }
func Compact(value string) string {
	folded := cases.Fold().String(norm.NFKC.String(value))
	return strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsNumber(r) {
			return r
		}
		return -1
	}, folded)
}
func Query(value string) (string, error) {
	value = strings.TrimSpace(value)
	if !utf8.ValidString(value) || value == "" || utf8.RuneCountInString(value) > 100 {
		return "", fmt.Errorf("%w: 搜索内容长度必须为 1 到 100 个字符", ErrQuery)
	}
	result := Compact(value)
	if result == "" {
		return "", fmt.Errorf("%w: 搜索内容必须包含文字或数字", ErrQuery)
	}
	return result, nil
}
func (e *Engine) Simplify(value string) (string, error) { return e.simplified.Convert(value) }

func han(r rune) bool {
	// pypinyin 0.55.0's RE_HANS, including its intentionally sparse extension
	// ranges. Do not silently extend this when Go upgrades its Unicode tables.
	return r == 0x3007 || r >= 0xe815 && r <= 0xe864 || r >= 0x3400 && r <= 0x4dbf || r >= 0x4e00 && r <= 0x9fff || r >= 0xf900 && r <= 0xfaff || r >= 0x20000 && r <= 0x2a6df || r >= 0x2a703 && r <= 0x2b73f || r >= 0x2b740 && r <= 0x2b81d || r >= 0x2b825 && r <= 0x2bf6e || r >= 0x2c029 && r <= 0x2ce93 || r == 0x2d016 || r >= 0x2d11b && r <= 0x2ebd9 || r >= 0x2f80a && r <= 0x2fa1f || r >= 0x30000 && r <= 0x3134a || r >= 0x31350 && r <= 0x32389
}

func (e *Engine) Pinyin(value string) string {
	runes := []rune(value)
	var result strings.Builder
	for i := 0; i < len(runes); {
		if !han(runes[i]) {
			result.WriteRune(runes[i])
			i++
			continue
		}
		current := e.phrases
		end := i
		phrase := ""
		for j := i; j < len(runes) && han(runes[j]); j++ {
			current = current.Next[runes[j]]
			if current == nil {
				break
			}
			if current.Value != "" {
				end = j + 1
				phrase = current.Value
			}
		}
		if end > i {
			result.WriteString(phrase)
			i = end
			continue
		}
		if pronunciation, ok := e.characters[runes[i]]; ok {
			result.WriteString(pronunciation)
		} else {
			result.WriteRune(runes[i])
		}
		i++
	}
	return result.String()
}

func (e *Engine) Text(values ...string) (string, error) {
	aliases := map[string]bool{}
	for _, value := range values {
		value = cases.Fold().String(norm.NFKC.String(value))
		simplified, err := e.simplified.Convert(value)
		if err != nil {
			return "", err
		}
		traditional, err := e.traditional.Convert(value)
		if err != nil {
			return "", err
		}
		for _, variant := range []string{value, simplified, traditional} {
			compact := Compact(variant)
			if compact == "" {
				continue
			}
			aliases[compact] = true
			aliases[Compact(e.Pinyin(variant))] = true
		}
	}
	keys := make([]string, 0, len(aliases))
	for value := range aliases {
		keys = append(keys, value)
	}
	sort.Strings(keys)
	return strings.Join(keys, " "), nil
}
