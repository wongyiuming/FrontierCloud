//go:build ignore

// Mechanically imports the existing MIT-licensed pronunciation DATA, not Python
// source. Re-run only when intentionally changing the pinned search contract.
package main

import (
	"compress/gzip"
	"crypto/sha256"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

func normal(s string) string {
	// Exact pypinyin Style.NORMAL substitutions; ê must NOT become e.
	r := strings.NewReplacer("ā", "a", "á", "a", "ǎ", "a", "à", "a", "ē", "e", "é", "e", "ě", "e", "è", "e", "ō", "o", "ó", "o", "ǒ", "o", "ò", "o", "ī", "i", "í", "i", "ǐ", "i", "ì", "i", "ū", "u", "ú", "u", "ǔ", "u", "ù", "u", "ü", "v", "ǖ", "v", "ǘ", "v", "ǚ", "v", "ǜ", "v", "ń", "n", "ň", "n", "ǹ", "n", "m̄", "m", "ḿ", "m", "m̀", "m", "ê̄", "ê", "ế", "ê", "ê̌", "ê", "ề", "ê")
	return strings.Map(func(c rune) rune {
		if c >= '0' && c <= '9' {
			return -1
		}
		return c
	}, r.Replace(s))
}

func main() {
	input := flag.String("input", "", "directory containing pinyin_dict.json and phrases_dict.json")
	output := flag.String("output", "internal/search/data/pinyin.json.gz", "generated data asset")
	flag.Parse()
	if *input == "" {
		panic("input directory required")
	}
	data := struct {
		Source     string            `json:"source"`
		Hashes     map[string]string `json:"sha256"`
		Characters map[string]string `json:"characters"`
		Phrases    map[string]string `json:"phrases"`
	}{Source: "pypinyin 0.55.0 (MIT)", Hashes: map[string]string{}, Characters: map[string]string{}, Phrases: map[string]string{}}
	read := func(name string) []byte {
		b, err := os.ReadFile(filepath.Join(*input, name))
		if err != nil {
			panic(err)
		}
		data.Hashes[name] = fmt.Sprintf("%x", sha256.Sum256(b))
		return b
	}
	var characters map[string]string
	if err := json.Unmarshal(read("pinyin_dict.json"), &characters); err != nil {
		panic(err)
	}
	for key, v := range characters {
		n, err := strconv.ParseInt(key, 10, 32)
		if err != nil {
			panic(err)
		}
		data.Characters[string(rune(n))] = normal(strings.Split(v, ",")[0])
	}
	var phrases map[string][][]string
	if err := json.Unmarshal(read("phrases_dict.json"), &phrases); err != nil {
		panic(err)
	}
	for key, values := range phrases {
		var b strings.Builder
		for _, v := range values {
			if len(v) == 0 {
				panic("empty pronunciation")
			}
			b.WriteString(normal(v[0]))
		}
		data.Phrases[key] = b.String()
	}
	encoded, err := json.Marshal(data)
	if err != nil {
		panic(err)
	}
	if err := os.MkdirAll(filepath.Dir(*output), 0755); err != nil {
		panic(err)
	}
	f, err := os.Create(*output)
	if err != nil {
		panic(err)
	}
	z, err := gzip.NewWriterLevel(f, gzip.BestCompression)
	if err != nil {
		panic(err)
	}
	if _, err = z.Write(encoded); err != nil {
		panic(err)
	}
	if err = z.Close(); err != nil {
		panic(err)
	}
	if err = f.Close(); err != nil {
		panic(err)
	}
	fmt.Printf("Imported %d characters and %d phrases; source hashes: %v\n", len(data.Characters), len(data.Phrases), data.Hashes)
}
