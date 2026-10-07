from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.db import engine
from app.store.database import write_transaction
from app.store.media_objects import media_objects_repository


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
    return await media_objects_repository(conn).legacy_has_data(object_kind, legacy_id)


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
    repository = media_objects_repository(conn)
    existing = await repository.find_path(locator, normalized)
    if existing:
        return str(existing)

    legacy_id = legacy_object_id(normalized)
    media_id = (
        legacy_id
        if await _legacy_id_has_business_data(conn, object_kind, legacy_id)
        else secrets.token_hex(32)
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await repository.register({
        "media_id": media_id, "object_kind": object_kind, "media_path": normalized,
        "path_locator": locator, "now": now,
    })
    resolved = await repository.find_path(locator, normalized, current=True)
    if not resolved:
        raise RuntimeError("Could not register media object")
    return str(resolved)


async def ensure_objects(items: Iterable[tuple[str, str]], database=None) -> dict[str, str]:
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

    async def lookup(conn: AsyncConnection, paths: list[str], *, current: bool = False) -> dict[str, str]:
        found: dict[str, str] = {}
        locators = {_path_locator(path): path for path in paths}
        for locator_chunk in chunks(sorted(locators)):
            rows = await media_objects_repository(conn).find_locators(locator_chunk, current=current)
            for row in rows:
                path = str(row["media_path"])
                if locators.get(str(row["path_locator"])) == path:
                    found[path] = str(row["media_id"])
        return found

    paths = sorted(requested_by_path, key=_path_locator)
    database = database or engine
    async with write_transaction(database) as conn:
        resolved = await lookup(conn, paths)
        missing = [path for path in paths if path not in resolved]
        legacy_ids = {path: legacy_object_id(path) for path in missing}
        business_ids: set[str] = set()
        for id_chunk in chunks(list(legacy_ids.values())):
            business_ids.update(await media_objects_repository(conn).legacy_business_ids(id_chunk))

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
            await media_objects_repository(conn).register(registrations)
            # Read the winner of a concurrent path registration.
            resolved.update(await lookup(conn, missing, current=True))
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


async def object_by_id(media_id: str) -> dict[str, str] | None:
    """Resolve one stable local object identity without accepting a path."""
    if len(media_id) != 64 or any(character not in "0123456789abcdef" for character in media_id):
        return None
    async with engine.connect() as conn:
        return await media_objects_repository(conn).find_id(media_id)
