from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.db import engine
from app.services import media_objects, media_search


BASE_DIR = Path(__file__).resolve().parents[2]
MEDIA_ROOT = (BASE_DIR / "data" / "media").resolve()
MUSIC_ROOT = (MEDIA_ROOT / "music").resolve()
LYRICS_ROOT = (MEDIA_ROOT / "lyrics").resolve()
AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav"}
LYRIC_EXTS = {".lrc"}
MAX_LYRIC_LINES = 10_000
MAX_LYRIC_LINE_LENGTH = 4_000
LRC_TIME_TAG = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[\.:](\d{1,3}))?\]")
LRC_OFFSET_TAG = re.compile(r"\[offset:([+-]?\d+)\]", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_lrc_bytes(payload: bytes) -> list[dict[str, Any]]:
    if not payload:
        raise ValueError("歌词文件不能为空")
    try:
        content = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("歌词文件必须使用 UTF-8 编码") from exc
    if "\x00" in content:
        raise ValueError("歌词文件包含非法字符")

    offset_match = LRC_OFFSET_TAG.search(content)
    offset_seconds = int(offset_match.group(1)) / 1000 if offset_match else 0.0
    entries: list[dict[str, Any]] = []
    seen: set[tuple[float, str]] = set()
    for raw_line in content.splitlines():
        matches = list(LRC_TIME_TAG.finditer(raw_line))
        if not matches:
            continue
        lyric_text = LRC_TIME_TAG.sub("", raw_line).strip()
        if not lyric_text:
            continue
        if len(lyric_text) > MAX_LYRIC_LINE_LENGTH:
            raise ValueError(f"单行歌词最多允许 {MAX_LYRIC_LINE_LENGTH} 个字符")
        for match in matches:
            fraction = match.group(3) or ""
            seconds = int(match.group(1)) * 60 + int(match.group(2))
            if fraction:
                seconds += int(fraction) / (10 ** len(fraction))
            timestamp = round(max(0.0, seconds + offset_seconds), 3)
            identity = (timestamp, lyric_text)
            if identity in seen:
                continue
            seen.add(identity)
            entries.append({"time": timestamp, "text": lyric_text})

    if not entries:
        raise ValueError("LRC 歌词没有可展示的时间轴内容")
    if len(entries) > MAX_LYRIC_LINES:
        raise ValueError(f"歌词最多允许 {MAX_LYRIC_LINES} 行")
    entries.sort(key=lambda entry: entry["time"])
    return entries


def _safe_file(relative_path: str, root_name: str, extensions: set[str]) -> tuple[str, Path]:
    normalized = str(relative_path or "").replace("\\", "/").strip().lstrip("/")
    candidate = (MEDIA_ROOT / normalized).resolve()
    parts = candidate.relative_to(MEDIA_ROOT).parts if candidate.is_relative_to(MEDIA_ROOT) else ()
    expected_depths = {3, 4} if root_name == "music" else {2}
    if (
        not normalized
        or len(parts) not in expected_depths
        or parts[0] != root_name
        or candidate.is_symlink()
        or not candidate.is_file()
        or candidate.suffix.lower() not in extensions
    ):
        raise ValueError("曲目或歌词文件无效")
    return candidate.relative_to(MEDIA_ROOT).as_posix(), candidate


def validate_track(relative_path: str) -> tuple[str, Path]:
    return _safe_file(relative_path, "music", AUDIO_EXTS)


def validate_lyric(relative_path: str) -> tuple[str, Path]:
    return _safe_file(relative_path, "lyrics", LYRIC_EXTS)


def _catalog_scope(relative_scope: str, kind: str) -> tuple[str, Path]:
    normalized = str(relative_scope or "").replace("\\", "/").strip().strip("/")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    root_name = "music" if kind == "track" else "lyrics"
    max_depth = 3 if kind == "track" else 1
    if (
        not parts
        or parts[0] != root_name
        or len(parts) > max_depth
        or any(part == ".." or "\x00" in part for part in parts)
    ):
        raise ValueError(
            "禁止在 data/media 执行全局查询，请在 music 或 lyrics 文件树内选择目录"
        )
    current = (MEDIA_ROOT / normalized).resolve()
    if (
        not current.is_relative_to(MEDIA_ROOT)
        or not current.exists()
        or not current.is_dir()
        or current.is_symlink()
    ):
        raise ValueError("查询目录不存在")
    return current.relative_to(MEDIA_ROOT).as_posix(), current


def _scan_catalog_scope_sync(
    relative_scope: str,
    current: Path,
    kind: str,
    query: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int, bool]:
    extensions = AUDIO_EXTS if kind == "track" else LYRIC_EXTS
    valid_depths = {3, 4} if kind == "track" else {2}
    directories = [
        {
            "name": child.name,
            "path": child.relative_to(MEDIA_ROOT).as_posix(),
        }
        for child in current.iterdir()
        if (
            child.is_dir()
            and not child.is_symlink()
            and not child.name.startswith(".")
            and len(child.relative_to(MEDIA_ROOT).parts) <= (3 if kind == "track" else 1)
        )
    ]
    directories.sort(key=lambda item: item["name"].casefold())
    matches: list[dict[str, Any]] = []
    total = 0
    for path in current.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name.startswith(".")
            or path.suffix.lower() not in extensions
            or len(path.relative_to(MEDIA_ROOT).parts) not in valid_depths
        ):
            continue
        total += 1
        relative_path = path.relative_to(MEDIA_ROOT).as_posix()
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


async def catalog(
    track_scope: str = "music",
    lyric_scope: str = "lyrics",
    track_query: str = "",
    lyric_query: str = "",
) -> dict[str, Any]:
    normalized_track_query = media_search.normalized_query(track_query) if track_query else ""
    normalized_lyric_query = media_search.normalized_query(lyric_query) if lyric_query else ""
    track_scope, track_root = _catalog_scope(track_scope, "track")
    lyric_scope, lyric_root = _catalog_scope(lyric_scope, "lyric")
    track_scan, lyric_scan = await asyncio.gather(
        asyncio.to_thread(
            _scan_catalog_scope_sync,
            track_scope,
            track_root,
            "track",
            normalized_track_query,
        ),
        asyncio.to_thread(
            _scan_catalog_scope_sync,
            lyric_scope,
            lyric_root,
            "lyric",
            normalized_lyric_query,
        ),
    )
    tracks, track_directories, track_total, track_truncated = track_scan
    lyric_files, lyric_directories, lyric_total, lyric_truncated = lyric_scan
    tracks = await media_objects.bind_items(
        tracks, "audio", path_key="path", id_key="media_id"
    )
    lyric_files = await media_objects.bind_items(
        lyric_files, "lyric", path_key="path", id_key="lyric_id"
    )
    valid_tracks = {item["path"] for item in tracks}
    valid_lyrics = {item["path"] for item in lyric_files}
    async with engine.connect() as conn:
        relation_count = await conn.scalar(text("""
            SELECT COUNT(*)
            FROM media_lyric_links AS link
            INNER JOIN media_objects AS media_object ON media_object.media_id=link.media_id
            INNER JOIN media_objects AS lyric_object ON lyric_object.media_id=link.lyric_id
            WHERE LEFT(media_object.media_path, CHAR_LENGTH(:track_scope))=:track_scope
              AND SUBSTRING(media_object.media_path, CHAR_LENGTH(:track_scope) + 1, 1)='/'
              AND LEFT(lyric_object.media_path, CHAR_LENGTH(:lyric_scope))=:lyric_scope
              AND SUBSTRING(lyric_object.media_path, CHAR_LENGTH(:lyric_scope) + 1, 1)='/'
        """), {
            "track_scope": track_scope,
            "lyric_scope": lyric_scope,
        })
        conditions: list[str] = []
        parameters: dict[str, str] = {}
        if valid_tracks:
            placeholders = []
            for index, path in enumerate(sorted(valid_tracks)):
                key = f"track_{index}"
                placeholders.append(f":{key}")
                parameters[key] = path
            conditions.append(f"media_object.media_path IN ({', '.join(placeholders)})")
        if valid_lyrics:
            placeholders = []
            for index, path in enumerate(sorted(valid_lyrics)):
                key = f"lyric_{index}"
                placeholders.append(f":{key}")
                parameters[key] = path
            conditions.append(f"lyric_object.media_path IN ({', '.join(placeholders)})")
        relation_rows = []
        if conditions:
            result = await conn.execute(text(f"""
            SELECT media_object.media_path, lyric_object.media_path AS lyric_path
            FROM media_lyric_links AS link
            INNER JOIN media_objects AS media_object ON media_object.media_id=link.media_id
            INNER JOIN media_objects AS lyric_object ON lyric_object.media_id=link.lyric_id
            WHERE {' OR '.join(conditions)}
            """), parameters)
            relation_rows = result.mappings().all()
    relations = [
        {"track": str(row["media_path"]), "lyric": str(row["lyric_path"])}
        for row in relation_rows
    ]
    linked_counts: dict[str, int] = {}
    relation_by_track = {}
    for relation in relations:
        relation_by_track[relation["track"]] = relation["lyric"]
        linked_counts[relation["lyric"]] = linked_counts.get(relation["lyric"], 0) + 1
    for item in tracks:
        item["lyric_path"] = relation_by_track.get(item["path"], "")
    for item in lyric_files:
        item["linked_count"] = linked_counts.get(item["path"], 0)
    return {
        "scopes": {"track": track_scope, "lyric": lyric_scope},
        "counts": {"tracks": track_total, "lyrics": lyric_total, "relations": int(relation_count or 0)},
        "truncated": {"track": track_truncated, "lyric": lyric_truncated},
        "track_directories": track_directories,
        "lyric_directories": lyric_directories,
        "tracks": tracks,
        "lyrics": lyric_files,
        "relations": relations,
    }


async def replace_relations(origin_kind: str, origin_path: str, linked_paths: list[str]) -> int:
    if origin_kind not in {"track", "lyric"}:
        raise ValueError("关联起点类型无效")
    if not isinstance(linked_paths, list) or len(linked_paths) > 10_000:
        raise ValueError("关联目标无效")
    normalized_targets = list(dict.fromkeys(str(path) for path in linked_paths))
    if origin_kind == "track" and len(normalized_targets) > 1:
        raise ValueError("一首曲目最多关联一份歌词")

    if origin_kind == "track":
        origin_path, _ = validate_track(origin_path)
        normalized_targets = [validate_lyric(path)[0] for path in normalized_targets]
    else:
        origin_path, _ = validate_lyric(origin_path)
        normalized_targets = [validate_track(path)[0] for path in normalized_targets]

    from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock
    async with media_mutation_lock:
        ensure_media_mutations_ready()
        if origin_kind == "track":
            validate_track(origin_path)
            for path in normalized_targets:
                validate_lyric(path)
        else:
            validate_lyric(origin_path)
            for path in normalized_targets:
                validate_track(path)
        now = _utcnow()
        async with engine.begin() as conn:
            if origin_kind == "track":
                origin_id = await media_objects.ensure_object(conn, origin_path, "audio")
                await conn.execute(
                    text("DELETE FROM media_lyric_links WHERE media_id=:origin_id"),
                    {"origin_id": origin_id},
                )
                pairs = [(origin_path, path) for path in normalized_targets]
            else:
                origin_id = await media_objects.ensure_object(conn, origin_path, "lyric")
                await conn.execute(
                    text("DELETE FROM media_lyric_links WHERE lyric_id=:origin_id"),
                    {"origin_id": origin_id},
                )
                pairs = [(path, origin_path) for path in normalized_targets]
            for track, lyric in pairs:
                media_id = await media_objects.ensure_object(conn, track, "audio")
                lyric_id = await media_objects.ensure_object(conn, lyric, "lyric")
                await conn.execute(text("""
                    INSERT INTO media_lyric_links
                    (media_id, media_path, lyric_id, lyric_path, created_at, updated_at)
                    VALUES (:media_id, :media_path, :lyric_id, :lyric_path, :now, :now)
                    ON DUPLICATE KEY UPDATE
                        media_path=VALUES(media_path), lyric_id=VALUES(lyric_id),
                        lyric_path=VALUES(lyric_path), updated_at=VALUES(updated_at)
                """), {
                    "media_id": media_id,
                    "media_path": track,
                    "lyric_id": lyric_id,
                    "lyric_path": lyric,
                    "now": now,
                })
    return len(pairs)


async def attach_links(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = [dict(item) for item in items]
    media_ids = [str(item["media_id"]) for item in enriched]
    links: dict[str, str] = {}
    if media_ids:
        placeholders = ", ".join(f":media_{index}" for index in range(len(media_ids)))
        params = {f"media_{index}": value for index, value in enumerate(media_ids)}
        async with engine.connect() as conn:
            result = await conn.execute(text(
                "SELECT media_id, lyric_path FROM media_lyric_links "
                f"WHERE media_id IN ({placeholders})"
            ), params)
        links = {
            str(row["media_id"]): str(row["lyric_path"])
            for row in result.mappings().all()
            if _existing_lyric_path(str(row["lyric_path"]))
        }
    for item in enriched:
        item["has_lyrics"] = str(item["media_id"]) in links
    return enriched


async def load_for_track(track_path: str) -> tuple[str, list[dict[str, Any]]]:
    normalized_track, _ = validate_track(track_path)
    async with engine.begin() as conn:
        media_id = await media_objects.ensure_object(conn, normalized_track, "audio")
        lyric_path = await conn.scalar(text("""
            SELECT lyric_object.media_path
            FROM media_lyric_links AS link
            INNER JOIN media_objects AS lyric_object ON lyric_object.media_id=link.lyric_id
            WHERE link.media_id=:media_id
        """), {"media_id": media_id})
    if not lyric_path:
        raise FileNotFoundError("曲目没有关联歌词")
    normalized_lyric, path = validate_lyric(str(lyric_path))
    payload = await asyncio.to_thread(path.read_bytes)
    entries = await asyncio.to_thread(parse_lrc_bytes, payload)
    return normalized_lyric, entries


def _existing_lyric_path(relative_path: str) -> bool:
    try:
        validate_lyric(relative_path)
        return True
    except ValueError:
        return False
