"""Add recursive file counts to Admin lyric-relation directory entries."""
from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import text

from app.services import lyrics
from app.services.federation.state import state as node_state


TRACK_FILE_DEPTHS = frozenset({3, 4})
LYRIC_FILE_DEPTHS = frozenset({2, 3, 4})


def _count_under(directories: list[dict], paths: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    normalized_paths = [str(path).replace("\\", "/").strip("/") for path in paths]
    for item in directories:
        directory = str(item.get("path") or "").replace("\\", "/").strip("/")
        prefix = directory + "/"
        counts[directory] = sum(path.startswith(prefix) for path in normalized_paths)
    return counts


def _local_supported_paths(scope: str, kind: str) -> list[str]:
    from app.services.media_manager import resolve_safe_path

    current = resolve_safe_path(lyrics.MEDIA_ROOT, scope)
    if not current.exists() or not current.is_dir() or current.is_symlink():
        return []

    extensions = lyrics.AUDIO_EXTS if kind == "track" else lyrics.LYRIC_EXTS
    valid_depths = TRACK_FILE_DEPTHS if kind == "track" else LYRIC_FILE_DEPTHS
    result: list[str] = []
    for path in current.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.name.startswith("."):
            continue
        relative = path.relative_to(lyrics.MEDIA_ROOT)
        normalized = relative.as_posix()
        if (
            len(relative.parts) not in valid_depths
            or path.suffix.lower() not in extensions
            or (kind == "lyric" and normalized == lyrics.DEFAULT_LYRIC_PATH)
        ):
            continue
        result.append(normalized)
    return result


async def _master_track_paths(scope: str) -> list[str]:
    prefix = scope.rstrip("/") + "/"
    async with lyrics.engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT media_path
            FROM global_media_objects
            WHERE object_kind='audio'
              AND state='active'
              AND LEFT(media_path, CHAR_LENGTH(:prefix))=:prefix
        """), {"prefix": prefix})).scalars().all()
    return [str(path) for path in rows]


async def _directory_paths(scope: str, kind: str) -> list[str]:
    if kind == "track" and node_state.node.get("role") == "Master":
        return await _master_track_paths(scope)
    return await asyncio.to_thread(_local_supported_paths, scope, kind)


async def enrich_catalog_directory_counts(result: dict) -> dict:
    enriched = dict(result)
    track_directories = [dict(item) for item in result.get("track_directories", [])]
    lyric_directories = [dict(item) for item in result.get("lyric_directories", [])]

    track_paths, lyric_paths = await asyncio.gather(
        _directory_paths(str(result.get("scopes", {}).get("track") or "music"), "track"),
        _directory_paths(str(result.get("scopes", {}).get("lyric") or "lyrics"), "lyric"),
    )
    track_counts = _count_under(track_directories, track_paths)
    lyric_counts = _count_under(lyric_directories, lyric_paths)

    for item in track_directories:
        item["count"] = track_counts.get(str(item.get("path") or ""), 0)
    for item in lyric_directories:
        item["count"] = lyric_counts.get(str(item.get("path") or ""), 0)

    enriched["track_directories"] = track_directories
    enriched["lyric_directories"] = lyric_directories
    return enriched


def install() -> None:
    if getattr(lyrics, "_directory_counts_installed", False):
        return

    original_catalog = lyrics.catalog

    async def catalog(
        track_scope: str = "music",
        lyric_scope: str = "lyrics",
        track_query: str = "",
        lyric_query: str = "",
    ) -> dict:
        result = await original_catalog(track_scope, lyric_scope, track_query, lyric_query)
        return await enrich_catalog_directory_counts(result)

    lyrics.catalog = catalog
    lyrics._directory_counts_installed = True
