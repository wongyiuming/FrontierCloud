from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.db import engine


OBJECT_KINDS = frozenset({"audio", "video", "lyric"})


def normalize_object_path(relative_path: str) -> str:
    normalized = str(relative_path or "").replace("\\", "/").strip().lstrip("/")
    if not normalized or normalized.startswith("../") or "/../" in f"/{normalized}/":
        raise ValueError("Invalid media object path")
    return normalized


def _path_locator(relative_path: str) -> str:
    return hashlib.sha256(normalize_object_path(relative_path).encode("utf-8")).hexdigest()


def legacy_object_id(relative_path: str) -> str:
    """Return the pre-registry path ID solely for preserving existing business data."""
    return _path_locator(relative_path)


async def _legacy_id_has_business_data(
    conn: AsyncConnection,
    object_kind: str,
    legacy_id: str,
) -> bool:
    if object_kind == "lyric":
        statement = "SELECT EXISTS(SELECT 1 FROM media_lyric_links WHERE lyric_id=:media_id)"
    else:
        statement = """
            SELECT (
                EXISTS(SELECT 1 FROM media_playback_stats WHERE media_id=:media_id)
                OR EXISTS(SELECT 1 FROM media_playback_events WHERE media_id=:media_id)
                OR EXISTS(SELECT 1 FROM media_lyric_links WHERE media_id=:media_id)
            )
        """
    return bool(await conn.scalar(text(statement), {"media_id": legacy_id}))


async def ensure_object(
    conn: AsyncConnection,
    relative_path: str,
    object_kind: str,
) -> str:
    """Resolve a path to a persistent object ID inside the caller's transaction."""
    if object_kind not in OBJECT_KINDS:
        raise ValueError("Invalid media object kind")
    normalized = normalize_object_path(relative_path)
    locator = _path_locator(normalized)
    existing = await conn.scalar(
        text("""
            SELECT media_id FROM media_objects
            WHERE path_locator=:path_locator AND BINARY media_path=BINARY :media_path
        """),
        {"path_locator": locator, "media_path": normalized},
    )
    if existing:
        return str(existing)

    legacy_id = legacy_object_id(normalized)
    media_id = (
        legacy_id
        if await _legacy_id_has_business_data(conn, object_kind, legacy_id)
        else secrets.token_hex(32)
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await conn.execute(
        text("""
            INSERT IGNORE INTO media_objects
            (media_id, object_kind, media_path, path_locator, created_at, updated_at)
            VALUES (:media_id, :object_kind, :media_path, :path_locator, :now, :now)
        """),
        {
            "media_id": media_id,
            "object_kind": object_kind,
            "media_path": normalized,
            "path_locator": locator,
            "now": now,
        },
    )
    resolved = await conn.scalar(
        text("""
            SELECT media_id FROM media_objects
            WHERE path_locator=:path_locator AND BINARY media_path=BINARY :media_path
        """),
        {"path_locator": locator, "media_path": normalized},
    )
    if not resolved:
        raise RuntimeError("Could not register media object")
    return str(resolved)


async def ensure_objects(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    requested_by_path: dict[str, str] = {}
    for raw_path, kind in items:
        path = normalize_object_path(raw_path)
        if kind not in OBJECT_KINDS:
            raise ValueError("Invalid media object kind")
        previous = requested_by_path.setdefault(path, kind)
        if previous != kind:
            raise ValueError("One media object path cannot have multiple kinds")
    if not requested_by_path:
        return {}

    def chunks(values: list[str], size: int = 500) -> Iterator[list[str]]:
        for offset in range(0, len(values), size):
            yield values[offset:offset + size]

    async def lookup(conn: AsyncConnection, paths: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        locators = {_path_locator(path): path for path in paths}
        for locator_chunk in chunks(list(locators)):
            placeholders = ", ".join(f":locator_{index}" for index in range(len(locator_chunk)))
            params = {f"locator_{index}": value for index, value in enumerate(locator_chunk)}
            result = await conn.execute(text(
                "SELECT media_id, media_path, path_locator FROM media_objects "
                f"WHERE path_locator IN ({placeholders})"
            ), params)
            for row in result.mappings().all():
                path = str(row["media_path"])
                if locators.get(str(row["path_locator"])) == path:
                    found[path] = str(row["media_id"])
        return found

    paths = list(requested_by_path)
    async with engine.begin() as conn:
        resolved = await lookup(conn, paths)
        missing = [path for path in paths if path not in resolved]
        legacy_ids = {path: legacy_object_id(path) for path in missing}
        business_ids: set[str] = set()
        for id_chunk in chunks(list(legacy_ids.values())):
            placeholders = ", ".join(f":media_{index}" for index in range(len(id_chunk)))
            params = {f"media_{index}": value for index, value in enumerate(id_chunk)}
            result = await conn.execute(text(f"""
                SELECT media_id AS object_id FROM media_playback_stats
                WHERE media_id IN ({placeholders})
                UNION SELECT media_id FROM media_playback_events
                WHERE media_id IN ({placeholders})
                UNION SELECT media_id FROM media_lyric_links
                WHERE media_id IN ({placeholders})
                UNION SELECT lyric_id FROM media_lyric_links
                WHERE lyric_id IN ({placeholders})
            """), params)
            business_ids.update(str(row[0]) for row in result.fetchall())

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        registrations = [
            {
                "media_id": legacy_ids[path] if legacy_ids[path] in business_ids else secrets.token_hex(32),
                "object_kind": requested_by_path[path],
                "media_path": path,
                "path_locator": _path_locator(path),
                "now": now,
            }
            for path in missing
        ]
        if registrations:
            await conn.execute(text("""
                INSERT IGNORE INTO media_objects
                (media_id, object_kind, media_path, path_locator, created_at, updated_at)
                VALUES (:media_id, :object_kind, :media_path, :path_locator, :now, :now)
            """), registrations)
            resolved.update(await lookup(conn, missing))
        if len(resolved) != len(paths):
            raise RuntimeError("Could not register every media object")
    return resolved


async def bind_items(
    items: Iterable[dict[str, Any]],
    object_kind: str,
    *,
    path_key: str = "media_path",
    id_key: str = "media_id",
) -> list[dict[str, Any]]:
    enriched = [dict(item) for item in items]
    identities = await ensure_objects(
        (str(item[path_key]), object_kind) for item in enriched
    )
    for item in enriched:
        item[id_key] = identities[normalize_object_path(str(item[path_key]))]
    return enriched
