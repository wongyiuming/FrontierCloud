"""Restore first-class nested lyric folders and hide the system fallback from Admin.

Folder uploads may preserve up to two relative directory levels below ``lyrics``
(e.g. ``lyrics/artist/album/song.lrc``).  The original lyric catalog still
accepted only ``lyrics/song.lrc``, which left valid folder uploads invisible.

The system ``default.lrc`` remains an internal fallback relation.  It must not
appear as a user lyric, count as a user lyric, or make a track look manually
linked in the Admin relation browser.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.services import lyrics, media_search


MAX_LYRIC_DIRECTORY_DEPTH = 3  # lyrics / artist / album
LYRIC_FILE_DEPTHS = {2, 3, 4}


def _safe_file(relative_path: str, root_name: str, extensions: set[str]) -> tuple[str, Path]:
    normalized = str(relative_path or "").replace("\\", "/").strip().lstrip("/")
    from app.services.media_manager import resolve_safe_path

    candidate = resolve_safe_path(lyrics.MEDIA_ROOT, normalized)
    parts = candidate.relative_to(lyrics.MEDIA_ROOT).parts if candidate.is_relative_to(lyrics.MEDIA_ROOT) else ()
    expected_depths = {3, 4} if root_name == "music" else LYRIC_FILE_DEPTHS
    if (
        not normalized
        or len(parts) not in expected_depths
        or parts[0] != root_name
        or candidate.is_symlink()
        or not candidate.is_file()
        or candidate.suffix.lower() not in extensions
    ):
        raise ValueError("曲目或歌词文件无效")
    return candidate.relative_to(lyrics.MEDIA_ROOT).as_posix(), candidate


def _catalog_scope(relative_scope: str, kind: str) -> tuple[str, Path]:
    normalized = str(relative_scope or "").replace("\\", "/").strip().strip("/")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    root_name = "music" if kind == "track" else "lyrics"
    max_depth = 3
    if (
        not parts
        or parts[0] != root_name
        or len(parts) > max_depth
        or any(part == ".." or "\x00" in part for part in parts)
    ):
        raise ValueError(
            "禁止在 data/media 执行全局查询，请在 music 或 lyrics 文件树内选择目录"
        )
    from app.services.media_manager import resolve_safe_path
    from app.services.federation.state import state as node_state

    current = resolve_safe_path(lyrics.MEDIA_ROOT, normalized)
    logical_master_track = kind == "track" and node_state.node.get("role") == "Master"
    if (
        not current.is_relative_to(lyrics.MEDIA_ROOT)
        or (not logical_master_track and not current.exists())
        or (current.exists() and (not current.is_dir() or current.is_symlink()))
    ):
        raise ValueError("查询目录不存在")
    return current.relative_to(lyrics.MEDIA_ROOT).as_posix(), current


def _scan_catalog_scope_sync(
    relative_scope: str,
    current: Path,
    kind: str,
    query: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int, bool]:
    extensions = lyrics.AUDIO_EXTS if kind == "track" else lyrics.LYRIC_EXTS
    valid_depths = {3, 4} if kind == "track" else LYRIC_FILE_DEPTHS
    directories = [
        {
            "name": child.name,
            "path": child.relative_to(lyrics.MEDIA_ROOT).as_posix(),
        }
        for child in current.iterdir()
        if (
            child.is_dir()
            and not child.is_symlink()
            and not child.name.startswith(".")
            and len(child.relative_to(lyrics.MEDIA_ROOT).parts) <= 3
        )
    ]
    directories.sort(key=lambda item: item["name"].casefold())
    if query:
        directories = []

    matches: list[dict[str, Any]] = []
    total = 0
    for path in current.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name.startswith(".")
            or path.suffix.lower() not in extensions
            or len(path.relative_to(lyrics.MEDIA_ROOT).parts) not in valid_depths
        ):
            continue
        relative_path = path.relative_to(lyrics.MEDIA_ROOT).as_posix()
        if kind == "lyric" and relative_path == lyrics.DEFAULT_LYRIC_PATH:
            continue
        total += 1
        search_text = media_search.build_search_text(path.stem, relative_path)
        if query:
            if not media_search.matches_search(search_text, query):
                continue
        elif path.parent != current:
            continue
        if len(matches) <= media_search.MAX_SEARCH_RESULTS:
            matches.append({"path": relative_path, "name": path.stem})
    matches.sort(key=lambda item: (item["name"].casefold(), item["path"].casefold()))
    truncated = len(matches) > media_search.MAX_SEARCH_RESULTS
    return matches[:media_search.MAX_SEARCH_RESULTS], directories, total, truncated


async def _visible_relation_count(track_scope: str, lyric_scope: str) -> int:
    from app.services.federation.state import state as node_state

    track_table = "global_media_objects" if node_state.node.get("role") == "Master" else "media_objects"
    async with lyrics.engine.connect() as conn:
        value = await conn.scalar(text(f"""
            SELECT COUNT(*)
            FROM media_lyric_links AS link
            INNER JOIN {track_table} AS media_object ON media_object.media_id=link.media_id
            INNER JOIN media_objects AS lyric_object ON lyric_object.media_id=link.lyric_id
            WHERE LEFT(media_object.media_path, CHAR_LENGTH(:track_scope))=:track_scope
              AND SUBSTRING(media_object.media_path, CHAR_LENGTH(:track_scope) + 1, 1)='/'
              AND LEFT(lyric_object.media_path, CHAR_LENGTH(:lyric_scope))=:lyric_scope
              AND SUBSTRING(lyric_object.media_path, CHAR_LENGTH(:lyric_scope) + 1, 1)='/'
              AND BINARY lyric_object.media_path != BINARY :default_lyric
        """), {
            "track_scope": track_scope,
            "lyric_scope": lyric_scope,
            "default_lyric": lyrics.DEFAULT_LYRIC_PATH,
        })
    return int(value or 0)


def _strip_system_fallback(result: dict[str, Any], relation_count: int) -> dict[str, Any]:
    result["lyrics"] = [
        item for item in result.get("lyrics", [])
        if item.get("path") != lyrics.DEFAULT_LYRIC_PATH
    ]
    result["relations"] = [
        relation for relation in result.get("relations", [])
        if relation.get("lyric") != lyrics.DEFAULT_LYRIC_PATH
    ]
    for track in result.get("tracks", []):
        if track.get("lyric_path") == lyrics.DEFAULT_LYRIC_PATH:
            track["lyric_path"] = None
    counts = result.setdefault("counts", {})
    counts["relations"] = relation_count
    # The corrected scanner already excludes default.lrc from lyric_total.  Use
    # the visible catalog as a lower bound for compatibility with scoped search.
    counts["lyrics"] = max(int(counts.get("lyrics", 0)), len(result["lyrics"]))
    return result


def install() -> None:
    if getattr(lyrics, "_nested_lyric_integrity_installed", False):
        return

    original_catalog = lyrics.catalog

    async def catalog(
        track_scope: str = "music",
        lyric_scope: str = "lyrics",
        track_query: str = "",
        lyric_query: str = "",
    ) -> dict[str, Any]:
        result = await original_catalog(track_scope, lyric_scope, track_query, lyric_query)
        relation_count = await _visible_relation_count(
            str(result["scopes"]["track"]),
            str(result["scopes"]["lyric"]),
        )
        return _strip_system_fallback(result, relation_count)

    lyrics._safe_file = _safe_file
    lyrics._catalog_scope = _catalog_scope
    lyrics._scan_catalog_scope_sync = _scan_catalog_scope_sync
    lyrics.catalog = catalog
    lyrics._nested_lyric_integrity_installed = True
