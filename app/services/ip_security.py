from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.admin_log import append_admin_log
from app.core.async_lock import LoopLocalAsyncLock
from app.core.client_ip import is_security_exempt, normalize_ip
from app.core.config import settings
from app.core.db import engine
from app.core.redis import redis_client


VIOLATION_PREFIX = "security:invalid-api:"
BAN_PREFIX = "security:auto-ban:"
RECENT_BANS_KEY = "security:auto-bans:recent"
WHITELIST_KEY = "security:ip-whitelist"
CACHE_READY_KEY = "security:cache-ready"
CACHE_LOCK_KEY = "security:cache-lock"
FIRST_BAN_SECONDS = 24 * 60 * 60
PERMANENT_EXPIRES_AT = datetime(9999, 12, 31, 23, 59, 59)
CACHE_LOCK_TIMEOUT_SECONDS = 120
CACHE_LOCK_WAIT_SECONDS = 30
CACHE_LOCK_RENEW_SECONDS = 40

_LOCAL_SECURITY_LOCK = LoopLocalAsyncLock()
_CACHE_LOCK: ContextVar[Any] = ContextVar("ip_security_cache_lock", default=None)
_MutationResult = TypeVar("_MutationResult")

_SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local cutoff_ms = now_ms - tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff_ms)
redis.call('ZADD', key, now_ms, ARGV[3])
local count = redis.call('ZCARD', key)
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
redis.call('EXPIRE', key, tonumber(ARGV[4]))
return {count, oldest[2] or now_ms}
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ban_key(ip: str) -> str:
    return BAN_PREFIX + ip


def _violation_key(ip: str) -> str:
    return VIOLATION_PREFIX + ip


async def _renew_security_lock(distributed_lock, owner_task: asyncio.Task) -> None:
    """Keep the distributed lease alive and abort the owner if it is lost."""
    while True:
        await asyncio.sleep(CACHE_LOCK_RENEW_SECONDS)
        try:
            await distributed_lock.extend(CACHE_LOCK_TIMEOUT_SECONDS, replace_ttl=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            append_admin_log(f"[IP_SECURITY] cache lock lease lost: {exc}")
            owner_task.cancel()
            return


@asynccontextmanager
async def _security_state_guard():
    """Serialize cache rebuilds and state transitions across tasks and workers."""
    async with _LOCAL_SECURITY_LOCK:
        distributed_lock = redis_client.lock(
            CACHE_LOCK_KEY,
            timeout=CACHE_LOCK_TIMEOUT_SECONDS,
            blocking_timeout=CACHE_LOCK_WAIT_SECONDS,
            thread_local=False,
            raise_on_release_error=False,
        )
        async with distributed_lock:
            owner_task = asyncio.current_task()
            if owner_task is None:
                raise RuntimeError("Security cache mutation has no owning task")
            renewal_task = asyncio.create_task(
                _renew_security_lock(distributed_lock, owner_task),
                name="ip-security-cache-lock-renewal",
            )
            context_token = _CACHE_LOCK.set(distributed_lock)
            try:
                yield
            finally:
                _CACHE_LOCK.reset(context_token)
                renewal_task.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal_task


async def _hydrate_ip_security_cache() -> None:
    """Replace the Redis projection with one coherent MySQL snapshot."""
    now = _utcnow()
    async with engine.connect() as conn:
        whitelist_rows = await conn.execute(text("SELECT ip_address FROM ip_permanent_whitelist"))
        ban_rows = await conn.execute(text("""
            SELECT ip_address, trigger_count, window_started_at, banned_at, expires_at,
                   last_method, last_path, status, ban_kind
            FROM ip_auto_ban_events
            WHERE status='active' AND expires_at > :now
            ORDER BY banned_at DESC
        """), {"now": now})
        recent_rows = await conn.execute(text("""
            SELECT ip_address, MAX(banned_at) AS banned_at
            FROM ip_auto_ban_events
            WHERE banned_at >= :cutoff
            GROUP BY ip_address
        """), {"cutoff": now - timedelta(hours=settings.SECURITY_RECENT_BAN_HOURS)})

    whitelist = [str(row[0]) for row in whitelist_rows.fetchall()]
    active_bans: dict[str, dict[str, Any]] = {}
    for row in ban_rows.mappings().all():
        active_bans.setdefault(str(row["ip_address"]), dict(row))

    stale_ban_keys = [key async for key in redis_client.scan_iter(match=BAN_PREFIX + "*")]
    pipe = redis_client.pipeline(transaction=True)
    distributed_lock = _CACHE_LOCK.get()
    if distributed_lock is not None:
        try:
            await pipe.watch(CACHE_LOCK_KEY)
            stored_token = await pipe.get(CACHE_LOCK_KEY)
            if isinstance(stored_token, str):
                stored_token = stored_token.encode()
            if stored_token is None or stored_token != distributed_lock.local.token:
                raise RedisError("Security cache lock ownership was lost")
            pipe.multi()
        except BaseException:
            await pipe.reset()
            raise
    pipe.delete(WHITELIST_KEY)
    pipe.delete(RECENT_BANS_KEY)
    if stale_ban_keys:
        pipe.delete(*stale_ban_keys)
    if whitelist:
        pipe.sadd(WHITELIST_KEY, *whitelist)
    for row in recent_rows.mappings().all():
        banned_timestamp = row["banned_at"].replace(tzinfo=timezone.utc).timestamp()
        pipe.zadd(RECENT_BANS_KEY, {str(row["ip_address"]): banned_timestamp})
    for ip, event in active_bans.items():
        permanent = str(event.get("ban_kind")) == "permanent"
        payload = {
            "ip": ip,
            "trigger_count": int(event["trigger_count"]),
            "window_started_at": event["window_started_at"].isoformat(),
            "banned_at": event["banned_at"].isoformat(),
            "expires_at": event["expires_at"].isoformat(),
            "last_method": event["last_method"],
            "last_path": event["last_path"],
            "ban_kind": event["ban_kind"],
            "permanent": permanent,
        }
        if permanent:
            pipe.set(_ban_key(ip), json.dumps(payload, ensure_ascii=False))
        else:
            expires_at = event["expires_at"].replace(tzinfo=timezone.utc)
            expires_at_ms = int(expires_at.timestamp() * 1000 + 0.999)
            pipe.set(_ban_key(ip), json.dumps(payload, ensure_ascii=False))
            pipe.pexpireat(_ban_key(ip), expires_at_ms)
    pipe.set(CACHE_READY_KEY, "1")
    await pipe.execute()


async def initialize_ip_security_cache() -> None:
    """Hydrate permanent whitelist and still-active bans from MySQL on startup."""
    async with _security_state_guard():
        await _hydrate_ip_security_cache()


async def ensure_ip_security_cache() -> None:
    """Rehydrate permanent state if Redis was flushed or restarted without its cache."""
    if await redis_client.exists(CACHE_READY_KEY):
        return
    async with _security_state_guard():
        if not await redis_client.exists(CACHE_READY_KEY):
            await _hydrate_ip_security_cache()


async def _refresh_after_failed_mutation() -> None:
    try:
        await _hydrate_ip_security_cache()
    except Exception as exc:
        append_admin_log(f"[IP_SECURITY] cache recovery deferred after rollback: {exc}")


async def _lock_ip_state(conn: AsyncConnection, ip: str) -> None:
    """Take the durable per-IP row lock for the rest of the MySQL transaction."""
    await conn.execute(text("""
        INSERT INTO ip_security_locks (ip_address)
        VALUES (:ip)
        ON DUPLICATE KEY UPDATE ip_address=VALUES(ip_address)
    """), {"ip": ip})


async def _run_state_transaction(
    operation: Callable[[AsyncConnection], Awaitable[_MutationResult]],
    *,
    clear_violations_for: str | None = None,
) -> _MutationResult:
    """Commit MySQL while Redis is dirty, then rebuild the cache projection."""
    async with _security_state_guard():
        await redis_client.delete(CACHE_READY_KEY)
        try:
            async with engine.begin() as conn:
                result = await operation(conn)
        except Exception:
            await _refresh_after_failed_mutation()
            raise

        try:
            if clear_violations_for is not None:
                await redis_client.delete(_violation_key(clear_violations_for))
            await _hydrate_ip_security_cache()
        except Exception as exc:
            # MySQL is authoritative.  Leaving CACHE_READY_KEY absent makes
            # request-time lookups fall back to MySQL until rehydration works.
            append_admin_log(
                "[IP_SECURITY] MySQL transition committed; Redis projection remains dirty: "
                f"{exc}"
            )
        return result


async def _mysql_block_fallback(ip: str) -> dict[str, Any] | None:
    now = _utcnow()
    async with engine.connect() as conn:
        whitelisted = await conn.scalar(
            text("SELECT 1 FROM ip_permanent_whitelist WHERE ip_address=:ip LIMIT 1"),
            {"ip": ip},
        )
        if whitelisted:
            return None
        result = await conn.execute(text("""
            SELECT ip_address, trigger_count, window_started_at, banned_at, expires_at,
                   last_method, last_path, ban_kind
            FROM ip_auto_ban_events
            WHERE ip_address=:ip AND status='active' AND expires_at > :now
            ORDER BY banned_at DESC LIMIT 1
        """), {"ip": ip, "now": now})
        row = result.mappings().first()
    if not row:
        return None
    return {
        "ip": str(row["ip_address"]),
        "trigger_count": int(row["trigger_count"]),
        "window_started_at": row["window_started_at"].isoformat(),
        "banned_at": row["banned_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "last_method": row["last_method"],
        "last_path": row["last_path"],
        "ban_kind": row["ban_kind"],
        "permanent": str(row["ban_kind"]) == "permanent",
    }


async def get_ip_block(ip: str) -> dict[str, Any] | None:
    if is_security_exempt(ip):
        return None
    try:
        await ensure_ip_security_cache()
        pipe = redis_client.pipeline(transaction=True)
        pipe.exists(CACHE_READY_KEY)
        pipe.sismember(WHITELIST_KEY, ip)
        pipe.get(_ban_key(ip))
        cache_ready, whitelisted, payload = await pipe.execute()
        if not cache_ready:
            return await _mysql_block_fallback(ip)
        if whitelisted or not payload:
            return None
        value = json.loads(payload)
        if not isinstance(value, dict):
            return None
        expires_at = datetime.fromisoformat(str(value["expires_at"]))
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
        if expires_at <= _utcnow():
            return None
        return value
    except (RedisError, SQLAlchemyError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        append_admin_log(f"[IP_SECURITY] Redis block lookup failed for {ip}; using MySQL: {exc}")
        try:
            return await _mysql_block_fallback(ip)
        except SQLAlchemyError as db_exc:
            append_admin_log(f"[IP_SECURITY] MySQL block lookup also failed for {ip}: {db_exc}")
            return None


async def record_invalid_api(ip: str, method: str, path: str, user_agent: str) -> int:
    if is_security_exempt(ip):
        return 0
    try:
        await ensure_ip_security_cache()
        if await redis_client.sismember(WHITELIST_KEY, ip):
            return 0
        now_ms = int(time.time() * 1000)
        result = await redis_client.eval(
            _SLIDING_WINDOW_SCRIPT,
            1,
            _violation_key(ip),
            now_ms,
            settings.SECURITY_INVALID_API_WINDOW * 1000,
            f"{now_ms}:{secrets.token_hex(6)}",
            settings.SECURITY_INVALID_API_WINDOW,
        )
        count = int(result[0])
        window_started_at = datetime.fromtimestamp(float(result[1]) / 1000, timezone.utc).replace(tzinfo=None)
    except (RedisError, SQLAlchemyError, TypeError, ValueError) as exc:
        append_admin_log(f"[IP_SECURITY] invalid API counter failed for {ip}: {exc}")
        return 0

    if count <= settings.SECURITY_INVALID_API_LIMIT:
        return count

    now = _utcnow()

    async def create_ban(conn: AsyncConnection) -> tuple[str, datetime] | None:
        await _lock_ip_state(conn, ip)
        await conn.execute(text("""
            UPDATE ip_auto_ban_events
            SET status='expired'
            WHERE ip_address=:ip AND status='active' AND expires_at <= :now
        """), {"ip": ip, "now": now})
        whitelisted = await conn.scalar(text("""
            SELECT 1 FROM ip_permanent_whitelist
            WHERE ip_address=:ip LIMIT 1 FOR UPDATE
        """), {"ip": ip})
        if whitelisted:
            return None
        active = await conn.scalar(text("""
            SELECT 1 FROM ip_auto_ban_events
            WHERE ip_address=:ip AND status='active' AND expires_at > :now
            LIMIT 1 FOR UPDATE
        """), {"ip": ip, "now": now})
        if active:
            return None
        previous_bans = int(await conn.scalar(text("""
            SELECT COUNT(*) FROM ip_auto_ban_events
            WHERE ip_address=:ip AND ban_kind IN ('auto', 'permanent')
        """), {"ip": ip}) or 0)
        permanent = previous_bans >= 1
        ban_kind = "permanent" if permanent else "auto"
        expires_at = PERMANENT_EXPIRES_AT if permanent else now + timedelta(seconds=FIRST_BAN_SECONDS)
        await conn.execute(text("""
            INSERT INTO ip_auto_ban_events
            (ip_address, trigger_count, window_started_at, banned_at, expires_at,
             last_method, last_path, user_agent, ban_kind, status)
            VALUES (:ip, :count, :window_start, :banned_at, :expires_at,
                    :method, :path, :ua, :ban_kind, 'active')
        """), {
            "ip": ip,
            "count": count,
            "window_start": window_started_at,
            "banned_at": now,
            "expires_at": expires_at,
            "method": method[:16],
            "path": path[:2048],
            "ua": user_agent[:512],
            "ban_kind": ban_kind,
        })
        return ban_kind, expires_at

    try:
        created = await _run_state_transaction(create_ban)
    except (RedisError, SQLAlchemyError) as exc:
        append_admin_log(f"[IP_SECURITY] failed to persist ban for {ip}: {exc}")
        return count

    if created is not None:
        ban_kind, expires_at = created
        append_admin_log(
            f"[IP_SECURITY] auto-banned ip={ip} count={count} "
            f"kind={ban_kind} expires_at={expires_at.isoformat()} last={method[:16]} {path[:512]}"
        )
    return count


async def unban_ip(ip_value: str, session_hash: str, status: str = "unbanned") -> str:
    ip = normalize_ip(ip_value)
    now = _utcnow()

    async def release_ban(conn: AsyncConnection) -> None:
        await _lock_ip_state(conn, ip)
        await conn.execute(text("""
            UPDATE ip_auto_ban_events
            SET status=:status, released_at=:now, released_by_session_hash=:session
            WHERE ip_address=:ip AND status='active'
        """), {"status": status, "now": now, "session": session_hash, "ip": ip})

    await _run_state_transaction(release_ban, clear_violations_for=ip)
    return ip


async def manual_ban_ip(ip_value: str, session_hash: str, reason: str) -> dict[str, Any]:
    ip = normalize_ip(ip_value)
    reason = str(reason or "").strip()
    if not reason or len(reason) > 255:
        raise ValueError("Manual ban reason is required")
    if is_security_exempt(ip):
        raise ValueError("Security exempt addresses cannot be banned")
    now = _utcnow()
    expires_at = now + timedelta(seconds=FIRST_BAN_SECONDS)

    async def create_manual_ban(conn: AsyncConnection) -> int:
        await _lock_ip_state(conn, ip)
        await conn.execute(text("""
            UPDATE ip_auto_ban_events
            SET status='expired'
            WHERE ip_address=:ip AND status='active' AND expires_at <= :now
        """), {"ip": ip, "now": now})
        whitelisted = await conn.scalar(text("""
            SELECT 1 FROM ip_permanent_whitelist
            WHERE ip_address=:ip LIMIT 1 FOR UPDATE
        """), {"ip": ip})
        if whitelisted:
            raise ValueError("Whitelisted addresses cannot be banned")
        active = await conn.scalar(text("""
            SELECT 1 FROM ip_auto_ban_events
            WHERE ip_address=:ip AND status='active' AND expires_at > :now
            LIMIT 1 FOR UPDATE
        """), {"ip": ip, "now": now})
        if active:
            raise ValueError("Address is already actively banned")
        inserted = await conn.execute(text("""
            INSERT INTO ip_auto_ban_events
            (ip_address, trigger_count, window_started_at, banned_at, expires_at,
             last_method, last_path, user_agent, ban_kind, reason,
             created_by_session_hash, status)
            VALUES (:ip, 0, :now, :now, :expires_at,
                    'ADMIN', 'manual-reban', NULL, 'manual', :reason,
                    :session_hash, 'active')
        """), {
            "ip": ip,
            "now": now,
            "expires_at": expires_at,
            "reason": reason,
            "session_hash": session_hash,
        })
        return int(inserted.lastrowid)

    event_id = await _run_state_transaction(create_manual_ban)
    append_admin_log(
        f"[IP_SECURITY] manually banned ip={ip} event_id={event_id} "
        f"expires_at={expires_at.isoformat()} reason={reason[:128]}"
    )
    return {"id": event_id, "ip": ip, "expires_at": expires_at.isoformat()}


async def manual_permanent_ban_ip(ip_value: str, session_hash: str, reason: str) -> dict[str, Any]:
    ip = normalize_ip(ip_value)
    reason = str(reason or "").strip()
    if not reason or len(reason) > 255:
        raise ValueError("Permanent ban reason is required")
    if is_security_exempt(ip):
        raise ValueError("Security exempt addresses cannot be banned")
    now = _utcnow()

    async def create_permanent_ban(conn: AsyncConnection) -> int:
        await _lock_ip_state(conn, ip)
        whitelisted = await conn.scalar(text("""
            SELECT 1 FROM ip_permanent_whitelist
            WHERE ip_address=:ip LIMIT 1 FOR UPDATE
        """), {"ip": ip})
        if whitelisted:
            raise ValueError("Whitelisted addresses cannot be banned")
        already_permanent = await conn.scalar(text("""
            SELECT 1 FROM ip_auto_ban_events
            WHERE ip_address=:ip AND status='active' AND ban_kind='permanent'
            LIMIT 1 FOR UPDATE
        """), {"ip": ip})
        if already_permanent:
            raise ValueError("Address is already permanently banned")
        await conn.execute(text("""
            UPDATE ip_auto_ban_events
            SET status='replaced', released_at=:now, released_by_session_hash=:session
            WHERE ip_address=:ip AND status='active'
        """), {"ip": ip, "now": now, "session": session_hash})
        inserted = await conn.execute(text("""
            INSERT INTO ip_auto_ban_events
            (ip_address, trigger_count, window_started_at, banned_at, expires_at,
             last_method, last_path, user_agent, ban_kind, reason,
             created_by_session_hash, status)
            VALUES (:ip, 0, :now, :now, :expires_at,
                    'ADMIN', 'manual-permanent-ban', NULL, 'permanent', :reason,
                    :session_hash, 'active')
        """), {
            "ip": ip,
            "now": now,
            "expires_at": PERMANENT_EXPIRES_AT,
            "reason": reason,
            "session_hash": session_hash,
        })
        return int(inserted.lastrowid)

    event_id = await _run_state_transaction(create_permanent_ban, clear_violations_for=ip)
    append_admin_log(
        f"[IP_SECURITY] permanently banned ip={ip} event_id={event_id} reason={reason[:128]}"
    )
    return {"id": event_id, "ip": ip, "permanent": True}


async def add_whitelist(ip_value: str, session_hash: str, note: str = "") -> str:
    ip = normalize_ip(ip_value)
    now = _utcnow()

    async def whitelist_and_release(conn: AsyncConnection) -> None:
        await _lock_ip_state(conn, ip)
        await conn.execute(text("""
            INSERT INTO ip_permanent_whitelist
            (ip_address, created_at, created_by_session_hash, note)
            VALUES (:ip, :now, :session, :note)
            ON DUPLICATE KEY UPDATE note=:note
        """), {"ip": ip, "now": now, "session": session_hash, "note": note[:255]})
        await conn.execute(text("""
            UPDATE ip_auto_ban_events
            SET status='whitelisted', released_at=:now, released_by_session_hash=:session
            WHERE ip_address=:ip AND status='active'
        """), {"now": now, "session": session_hash, "ip": ip})

    await _run_state_transaction(whitelist_and_release, clear_violations_for=ip)
    return ip


async def remove_whitelist(ip_value: str) -> str:
    ip = normalize_ip(ip_value)

    async def remove(conn: AsyncConnection) -> None:
        await _lock_ip_state(conn, ip)
        await conn.execute(text("DELETE FROM ip_permanent_whitelist WHERE ip_address=:ip"), {"ip": ip})

    await _run_state_transaction(remove, clear_violations_for=ip)
    return ip


async def list_security_history(
    ip_filter: str | None = None,
    status_filter: str | None = None,
    page: int = 1,
    page_size: int = 100,
    recent_only: bool = True,
) -> dict[str, Any]:
    now = _utcnow()
    cutoff = now - timedelta(hours=settings.SECURITY_RECENT_BAN_HOURS)
    conditions: list[str] = []
    params: dict[str, Any] = {
        "now": now,
        "cutoff": cutoff,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }
    if recent_only:
        conditions.append("banned_at >= :cutoff")
    if ip_filter:
        conditions.append("ip_address = :ip")
        params["ip"] = normalize_ip(ip_filter)
    if status_filter:
        allowed_statuses = {"active", "expired", "unbanned", "whitelisted", "replaced"}
        if status_filter not in allowed_statuses:
            raise ValueError("Invalid ban status")
        conditions.append("status = :status")
        params["status"] = status_filter
    where_sql = " WHERE " + " AND ".join(conditions) if conditions else ""

    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE ip_auto_ban_events
            SET status='expired'
            WHERE status='active' AND expires_at <= :now
        """), {"now": now})
        count_result = await conn.execute(
            text("SELECT COUNT(*) FROM ip_auto_ban_events" + where_sql),
            params,
        )
        total_events = int(count_result.scalar_one())
        active_count_result = await conn.execute(text("""
            SELECT COUNT(*) FROM ip_auto_ban_events
            WHERE status='active' AND expires_at > :now
        """), {"now": now})
        active_ban_count = int(active_count_result.scalar_one())
        ban_result = await conn.execute(text("""
            SELECT id, ip_address, trigger_count, window_started_at, banned_at, expires_at,
                   last_method, last_path, status, released_at, released_by_session_hash,
                   ban_kind, reason, created_by_session_hash
            FROM ip_auto_ban_events
        """ + where_sql + """
            ORDER BY banned_at DESC
            LIMIT :limit OFFSET :offset
        """), params)
        whitelist_result = await conn.execute(text("""
            SELECT ip_address, created_at, note
            FROM ip_permanent_whitelist
            ORDER BY created_at DESC
        """))

    whitelist_rows = whitelist_result.mappings().all()
    whitelist = {str(row["ip_address"]) for row in whitelist_rows}
    events = []
    for row in ban_result.mappings().all():
        expires_at = row["expires_at"]
        status = str(row["status"])
        active = status == "active" and expires_at > now and str(row["ip_address"]) not in whitelist
        events.append({
            "id": int(row["id"]),
            "ip": str(row["ip_address"]),
            "trigger_count": int(row["trigger_count"]),
            "window_started_at": row["window_started_at"].isoformat(),
            "banned_at": row["banned_at"].isoformat(),
            "expires_at": expires_at.isoformat(),
            "last_method": row["last_method"],
            "last_path": row["last_path"],
            "status": status,
            "active": active,
            "whitelisted": str(row["ip_address"]) in whitelist,
            "released_at": row["released_at"].isoformat() if row["released_at"] else None,
            "released_by_session_hash": row["released_by_session_hash"],
            "ban_kind": row["ban_kind"],
            "reason": row["reason"],
            "created_by_session_hash": row["created_by_session_hash"],
        })

    return {
        "events": events,
        "whitelist": [
            {
                "ip": str(row["ip_address"]),
                "created_at": row["created_at"].isoformat(),
                "note": row["note"] or "",
            }
            for row in whitelist_rows
        ],
        "active_ban_count": active_ban_count,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total_events,
            "pages": max(1, (total_events + page_size - 1) // page_size),
            "recent_only": recent_only,
        },
        "threshold": settings.SECURITY_INVALID_API_LIMIT,
        "window_seconds": settings.SECURITY_INVALID_API_WINDOW,
        "ban_seconds": FIRST_BAN_SECONDS,
        "second_offense_permanent": True,
    }


def legal_api_count(app: Any) -> int:
    pairs = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if not path.startswith("/api/") or not methods:
            continue
        for method in methods:
            if method not in {"HEAD", "OPTIONS"}:
                pairs.add((method, path))
    return len(pairs)
