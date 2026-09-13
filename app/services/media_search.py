from __future__ import annotations

import unicodedata
from functools import lru_cache

from opencc import OpenCC
from pypinyin import lazy_pinyin


MAX_SEARCH_QUERY_LENGTH = 100
MAX_SEARCH_RESULTS = 200
_to_simplified = OpenCC("t2s")
_to_traditional = OpenCC("s2t")


def simplify_filename(value: str) -> str:
    """Clean new upload names without touching existing managed objects."""
    return _to_simplified.convert(value)


def compact_search_text(value: str) -> str:
    """Normalize one query without applying expensive fuzzy matching."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


@lru_cache(maxsize=20_000)
def build_search_text(*values: str) -> str:
    """Build bounded original, Simplified, Traditional and pinyin aliases."""
    aliases: set[str] = set()
    for raw_value in values:
        value = unicodedata.normalize("NFKC", str(raw_value or "")).casefold()
        for variant in (value, _to_simplified.convert(value), _to_traditional.convert(value)):
            compact = compact_search_text(variant)
            if not compact:
                continue
            aliases.add(compact)
            aliases.add(compact_search_text("".join(lazy_pinyin(variant))))
    return " ".join(sorted(aliases))


def normalized_query(value: str) -> str:
    raw_value = str(value or "").strip()
    if not raw_value or len(raw_value) > MAX_SEARCH_QUERY_LENGTH:
        raise ValueError("搜索内容长度必须为 1 到 100 个字符")
    query = compact_search_text(raw_value)
    if not query:
        raise ValueError("搜索内容必须包含文字或数字")
    return query


def matches_search(search_text: str, query: str) -> bool:
    return query in str(search_text or "")
