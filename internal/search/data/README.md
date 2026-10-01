# Pinned search pronunciation data

`pinyin.json.gz` is a deterministic, mechanically transformed data asset from
the existing pypinyin 0.55.0 dependency (`pinyin_dict.json`, `phrases_dict.json`).
The embedded payload records the SHA-256 of both source files. It includes only
the first pronunciation in Style.NORMAL, preserving the existing search API's
phrase-aware maximum-forward-match semantics and non-Chinese characters.

Source: https://github.com/mozillazg/python-pinyin/tree/v0.55.0
License: MIT, reproduced in `LICENSE.pypinyin.txt`.

Import using Go (the input files are static JSON, not executable Python):

```
go run scripts/tools/import_pinyin.go -input <pypinyin-data-directory>
```

Production binaries embed this asset and require neither Python nor network
access to generate pronunciation aliases. Review conformance vectors whenever
updating this data or the OpenCC conversion dictionaries.
