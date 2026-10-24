from __future__ import annotations

import hashlib
import json
from typing import Any
from sqlalchemy import func,select
from sqlalchemy.exc import SQLAlchemyError

from redis.exceptions import RedisError

from app.core.admin_log import append_admin_log
from app.core.config import settings
from app.core.redis import redis_client


CACHE_PREFIX = "media:catalog:v2"
GENERATION_KEY = f"{CACHE_PREFIX}:generation"


def _cache_key(generation: int | str, namespace: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{CACHE_PREFIX}:{generation}:{namespace}:{digest}"


async def _s3_generation() -> str:
    from app.services.federation.state import state
    if state.node.get('role') != 'Master' or not getattr(state,'_s3_configured',False):
        return ''
    from app.services.s3_schema import members
    async with state.database.connect() as conn:
        rows = [list(row) for row in (await conn.execute(select(members.c.member_id,members.c.catalog_revision)
            .order_by(members.c.member_id).limit(65))).tuples()]
    if len(rows) > 64:
        raise ValueError('S3 cache namespace exceeds member limit')
    return hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest() if rows else ''


async def _migration_generation() -> str:
    from app.services.federation.state import state
    if state.node.get('role')!='Master':return ''
    from app.services.resource_migration_schema import tasks
    async with state.database.connect() as conn:
        count,total=(await conn.execute(select(func.count(),func.coalesce(func.sum(tasks.c.committed_bytes),0))
            .where(tasks.c.committed_bytes>0))).one()
    # Committed bytes never rewind and task history is retained. A cutover
    # changes this namespace in its own SQL transaction, even if Redis fails
    # or the runner exits before any post-commit cache notification. Native
    # acknowledgements, pauses and later replica cleanup do not invalidate it.
    return str(count)+':'+str(int(total)) if count else ''


async def load_media_catalog(namespace: str, identity: str) -> tuple[int | str | None, list[dict[str, Any]] | None]:
    """Return the current cache generation and a decoded catalog, if present."""
    try:
        generation = int(await redis_client.get(GENERATION_KEY) or 0)
        storage_generation = await _s3_generation()
        if storage_generation:
            generation = str(generation)+':s3:'+storage_generation
        migration_generation=await _migration_generation()
        if migration_generation:
            generation=str(generation)+':migration:'+migration_generation
        payload = await redis_client.get(_cache_key(generation, namespace, identity))
    except (RedisError, SQLAlchemyError, TypeError, ValueError) as exc:
        append_admin_log(f"[MEDIA_CACHE] namespace/read failed; using live catalog; error_type={type(exc).__name__}")
        return None, None

    if payload is None:
        return generation, None

    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return generation, None
    return (generation, value) if isinstance(value, list) else (generation, None)


async def store_media_catalog(
    generation: int | str | None,
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
