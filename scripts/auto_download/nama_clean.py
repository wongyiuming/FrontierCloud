"""Filename normalization helpers used by the media download workflows."""

from __future__ import annotations

import re
from pathlib import Path


_SEPARATOR_RE = re.compile(r"_+")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def sanitize_component(value: str, *, fallback: str = "untitled") -> str:
    """Return one portable path component while preserving Unicode letters."""
    normalized = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in str(value or "")
        if character != "\0"
    )
    normalized = _SEPARATOR_RE.sub("_", normalized).strip("_. ")
    normalized = normalized or fallback
    if normalized.upper() in _WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    return normalized


def sanitize_filename(
    original_name: str,
    *,
    parent_name: str | None = None,
    fallback: str = "untitled",
) -> str:
    """Clean a filename and optionally remove a repeated parent component."""
    path = Path(original_name)
    suffix = "".join(path.suffixes)
    stem = original_name[: -len(suffix)] if suffix else original_name
    clean_stem = sanitize_component(stem, fallback=fallback)

    if parent_name:
        clean_parent = sanitize_component(parent_name, fallback="")
        if clean_parent:
            clean_stem = clean_stem.replace(clean_parent, "")
            clean_stem = _SEPARATOR_RE.sub("_", clean_stem).strip("_") or fallback

    return f"{clean_stem}{suffix.lower()}"


def allocate_unique_stem(
    original_title: str,
    media_id: str,
    occupied: set[str],
) -> str:
    """Allocate a deterministic clean stem without overwriting another item."""
    clean_stem = sanitize_component(original_title)
    candidate = clean_stem
    if candidate.casefold() in occupied:
        candidate = f"{clean_stem}_{sanitize_component(media_id)}"
    occupied.add(candidate.casefold())
    return candidate
