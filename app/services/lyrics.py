from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.db import engine
from app.services.playback import media_id_for_path


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


def lyric_id_for_path(relative_path: str) -> str:
    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()


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


def _scan_catalog_sync() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tracks: list[dict[str, Any]] = []
    if MUSIC_ROOT.is_dir():
        for path in MUSIC_ROOT.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in AUDIO_EXTS:
                rel = path.relative_to(MEDIA_ROOT).as_posix()
                if len(path.relative_to(MEDIA_ROOT).parts) in {3, 4}:
                    tracks.append({"path": rel, "name": path.stem, "media_id": media_id_for_path(rel)})
    lyric_files: list[dict[str, Any]] = []
    if LYRICS_ROOT.is_dir():
        for path in LYRICS_ROOT.iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in LYRIC_EXTS:
                rel = path.relative_to(MEDIA_ROOT).as_posix()
                lyric_files.append({"path": rel, "name": path.stem, "lyric_id": lyric_id_for_path(rel)})
    tracks.sort(key=lambda item: (item["name"].casefold(), item["path"].casefold()))
    lyric_files.sort(key=lambda item: (item["name"].casefold(), item["path"].casefold()))
    return tracks, lyric_files


async def catalog() -> dict[str, Any]:
    tracks, lyric_files = await asyncio.to_thread(_scan_catalog_sync)
    valid_tracks = {item["path"] for item in tracks}
    valid_lyrics = {item["path"] for item in lyric_files}
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT media_path, lyric_path FROM media_lyric_links"))
        rows = result.mappings().all()
    relations = [
        {"track": str(row["media_path"]), "lyric": str(row["lyric_path"])}
        for row in rows
        if str(row["media_path"]) in valid_tracks and str(row["lyric_path"]) in valid_lyrics
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
        "counts": {"tracks": len(tracks), "lyrics": len(lyric_files), "relations": len(relations)},
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
                await conn.execute(
                    text("DELETE FROM media_lyric_links WHERE BINARY media_path=BINARY :origin"),
                    {"origin": origin_path},
                )
                pairs = [(origin_path, path) for path in normalized_targets]
            else:
                await conn.execute(
                    text("DELETE FROM media_lyric_links WHERE BINARY lyric_path=BINARY :origin"),
                    {"origin": origin_path},
                )
                pairs = [(path, origin_path) for path in normalized_targets]
            for track, lyric in pairs:
                await conn.execute(text("""
                    INSERT INTO media_lyric_links
                    (media_id, media_path, lyric_id, lyric_path, created_at, updated_at)
                    VALUES (:media_id, :media_path, :lyric_id, :lyric_path, :now, :now)
                    ON DUPLICATE KEY UPDATE
                        media_path=VALUES(media_path), lyric_id=VALUES(lyric_id),
                        lyric_path=VALUES(lyric_path), updated_at=VALUES(updated_at)
                """), {
                    "media_id": media_id_for_path(track),
                    "media_path": track,
                    "lyric_id": lyric_id_for_path(lyric),
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
    async with engine.connect() as conn:
        lyric_path = await conn.scalar(text(
            "SELECT lyric_path FROM media_lyric_links WHERE media_id=:media_id"
        ), {"media_id": media_id_for_path(normalized_track)})
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
