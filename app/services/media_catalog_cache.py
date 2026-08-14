from __future__ import annotations

import hashlib
import json
from typing import Any

from redis.exceptions import RedisError

from app.core.admin_log import append_admin_log
from app.core.config import settings
from app.core.redis import redis_client


CACHE_PREFIX = "media:catalog:v1"
GENERATION_KEY = f"{CACHE_PREFIX}:generation"


def _cache_key(generation: int, namespace: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{CACHE_PREFIX}:{generation}:{namespace}:{digest}"


async def load_media_catalog(namespace: str, identity: str) -> tuple[int | None, list[dict[str, Any]] | None]:
    """Return the current cache generation and a decoded catalog, if present."""
    try:
        generation = int(await redis_client.get(GENERATION_KEY) or 0)
        payload = await redis_client.get(_cache_key(generation, namespace, identity))
    except (RedisError, TypeError, ValueError) as exc:
        append_admin_log(f"[MEDIA_CACHE] read failed; using filesystem fallback: {exc}")
        return None, None

    if payload is None:
        return generation, None

    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return generation, None
    return (generation, value) if isinstance(value, list) else (generation, None)


async def store_media_catalog(
    generation: int | None,
    namespace: str,
    identity: str,
    value: list[dict[str, Any]],
) -> None:
    if generation is None:
        return
    try:
        await redis_client.set(
            _cache_key(generation, namespace, identity),
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            ex=settings.MEDIA_CATALOG_CACHE_TTL,
        )
    except RedisError as exc:
        append_admin_log(f"[MEDIA_CACHE] write failed; response remains available: {exc}")


async def invalidate_media_catalog() -> None:
    """Move readers to a new namespace; old entries disappear through their TTL."""
    try:
        await redis_client.incr(GENERATION_KEY)
    except RedisError as exc:
        append_admin_log(f"[MEDIA_CACHE] invalidation failed; cached data expires by TTL: {exc}")
