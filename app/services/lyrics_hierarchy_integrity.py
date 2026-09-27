"""Nested lyric hierarchy and system-fallback integrity contracts.

Lyrics mirror the supported music layout: ``lyrics/<category>/<subdir>/<file>.lrc``
is the deepest managed path.  Folder uploads, Admin tree/search/delete, lyric
catalog validation, and relation browsing must all use that same boundary.

``lyrics/default.lrc`` is an internal playback fallback.  It is intentionally
kept out of Admin listings, counts, search results, and user-managed mutation
surfaces.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from fastapi import HTTPException, UploadFile
from sqlalchemy import text

from app.services import lyrics, media_search


MAX_LYRIC_DIRECTORY_DEPTH = 3  # lyrics / category / subdir
LYRIC_FILE_DEPTHS = frozenset({2, 3, 4})
LYRIC_UPLOAD_RELATIVE_DEPTHS = frozenset({1, 2, 3})


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
        or any(part == ".." or "\x00" in part or part.startswith(".") for part in parts)
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
            and len(child.relative_to(lyrics.MEDIA_ROOT).parts) <= MAX_LYRIC_DIRECTORY_DEPTH
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
    counts["lyrics"] = max(int(counts.get("lyrics", 0)), len(result["lyrics"]))
    return result


def validate_upload_relative_path(relative_path: str | None, filename: str) -> str:
    """Return one supported lyric-upload path below ``lyrics`` or reject it."""
    from app.services.media_manager import MediaManager

    normalized = MediaManager.normalize_relative(relative_path or filename)
    parts = Path(normalized).parts
    if (
        len(parts) not in LYRIC_UPLOAD_RELATIVE_DEPTHS
        or any(part.startswith(".") for part in parts)
    ):
        raise HTTPException(
            status_code=400,
            detail="歌词目录最多支持两级：歌词分类/子目录/文件.lrc",
        )
    return normalized


async def _list_lyric_tree(relative_dir: str, original_list_tree) -> dict:
    from app.services import media_manager as manager

    rel = manager.MediaManager.normalize_relative(relative_dir) if relative_dir else ""
    if not rel or rel.split("/", 1)[0] != "lyrics":
        return await original_list_tree(relative_dir)
    try:
        scope, current = _catalog_scope(rel, "lyric")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    items: list[dict[str, Any]] = []
    for entry in sorted(current.iterdir(), key=lambda path: (not path.is_dir(), path.name.casefold())):
        if entry.is_symlink() or entry.name.startswith("."):
            continue
        relative_path = entry.relative_to(manager.MEDIA_ROOT).as_posix()
        parts = entry.relative_to(manager.MEDIA_ROOT).parts
        if entry.is_dir():
            if len(parts) > MAX_LYRIC_DIRECTORY_DEPTH:
                continue
            items.append({
                "name": entry.name,
                "path": relative_path,
                "kind": "directory",
                "size": None,
                "hidden": False,
                "media": False,
                "hideable": False,
            })
            continue
        if (
            relative_path == lyrics.DEFAULT_LYRIC_PATH
            or len(parts) not in LYRIC_FILE_DEPTHS
            or entry.suffix.lower() not in lyrics.LYRIC_EXTS
        ):
            continue
        items.append({
            "name": entry.name,
            "path": relative_path,
            "kind": "file",
            "size": entry.stat().st_size,
            "hidden": False,
            "media": False,
            "hideable": False,
        })
    return {"path": scope, "items": items}


def _lyric_search_scope(relative_scope: str, original_search_scope):
    normalized = str(relative_scope or "").replace("\\", "/").strip().strip("/")
    if not normalized or normalized.split("/", 1)[0] != "lyrics":
        return original_search_scope(relative_scope)
    try:
        scope, current = _catalog_scope(normalized, "lyric")
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return scope, current, set(lyrics.LYRIC_EXTS)


def _lyric_search_catalog_sync(scope: str, current: Path, extensions: set[str], hidden: set[str], original_scan):
    from app.services import media_manager as manager

    if scope.split("/", 1)[0] != "lyrics":
        return original_scan(scope, current, extensions, hidden)
    items: list[dict[str, Any]] = []
    for path in current.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.name.startswith("."):
            continue
        parts = path.relative_to(manager.MEDIA_ROOT).parts
        relative_path = path.relative_to(manager.MEDIA_ROOT).as_posix()
        if (
            len(parts) not in LYRIC_FILE_DEPTHS
            or path.suffix.lower() not in extensions
            or relative_path == lyrics.DEFAULT_LYRIC_PATH
        ):
            continue
        items.append({
            "name": path.name,
            "path": relative_path,
            "kind": "file",
            "size": path.stat().st_size,
            "hidden": False,
            "media": False,
            "hideable": False,
            "search_text": media_search.build_search_text(path.name, relative_path),
        })
    items.sort(key=lambda item: (item["name"].casefold(), item["path"].casefold()))
    return items


async def _collect_with_nested_lyrics(paths: Iterable[str], original_collect) -> list[tuple[str, Path]]:
    from app.services import media_manager as manager

    result: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for raw in paths:
        rel = manager.MediaManager.normalize_relative(str(raw))
        if rel in seen:
            continue
        seen.add(rel)
        if rel.split("/", 1)[0] != "lyrics":
            result.extend(await original_collect([rel]))
            continue
        if rel in {"lyrics", lyrics.DEFAULT_LYRIC_PATH}:
            raise HTTPException(status_code=400, detail="系统默认歌词和歌词根目录不能直接删除或下载")
        try:
            target = manager.resolve_safe_path(manager.MEDIA_ROOT, rel)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"非法对象路径: {rel}") from exc
        if not target.exists() or target.is_symlink():
            raise HTTPException(status_code=404, detail=f"对象不存在: {rel}")
        parts = target.relative_to(manager.MEDIA_ROOT).parts
        valid_directory = target.is_dir() and 2 <= len(parts) <= MAX_LYRIC_DIRECTORY_DEPTH
        valid_file = (
            target.is_file()
            and len(parts) in LYRIC_FILE_DEPTHS
            and target.suffix.lower() in lyrics.LYRIC_EXTS
        )
        if not valid_directory and not valid_file:
            raise HTTPException(status_code=400, detail=f"对象不在受支持的歌词层级内: {rel}")
        result.append((rel, target))
    return result


async def _upload_lyric_with_boundary(upload: UploadFile, relative_path: str | None, original_upload, *, audit=None) -> str:
    normalized = validate_upload_relative_path(relative_path, upload.filename or "")
    return await original_upload(upload, normalized, audit=audit)


def install() -> None:
    if getattr(lyrics, "_nested_lyric_integrity_installed", False):
        return

    from app.services.media_manager import MediaManager

    original_catalog = lyrics.catalog
    original_list_tree = MediaManager.list_tree
    original_search_scope = MediaManager._search_scope
    original_search_catalog_sync = MediaManager._search_catalog_sync
    original_collect = MediaManager._collect
    original_upload_lyric = MediaManager.upload_lyric

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

    async def list_tree(relative_dir: str = "") -> dict:
        return await _list_lyric_tree(relative_dir, original_list_tree)

    def search_scope(relative_scope: str):
        return _lyric_search_scope(relative_scope, original_search_scope)

    def search_catalog_sync(scope: str, current: Path, extensions: set[str], hidden: set[str]):
        return _lyric_search_catalog_sync(scope, current, extensions, hidden, original_search_catalog_sync)

    async def collect(paths: Iterable[str]) -> list[tuple[str, Path]]:
        return await _collect_with_nested_lyrics(paths, original_collect)

    async def upload_lyric(upload: UploadFile, relative_path: str | None = None, *, audit=None) -> str:
        return await _upload_lyric_with_boundary(
            upload, relative_path, original_upload_lyric, audit=audit,
        )

    lyrics._safe_file = _safe_file
    lyrics._catalog_scope = _catalog_scope
    lyrics._scan_catalog_scope_sync = _scan_catalog_scope_sync
    lyrics.catalog = catalog
    MediaManager.list_tree = staticmethod(list_tree)
    MediaManager._search_scope = staticmethod(search_scope)
    MediaManager._search_catalog_sync = staticmethod(search_catalog_sync)
    MediaManager._collect = staticmethod(collect)
    MediaManager.upload_lyric = staticmethod(upload_lyric)
    lyrics._nested_lyric_integrity_installed = True
