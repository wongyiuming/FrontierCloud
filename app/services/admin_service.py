from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request, Response
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.async_lock import LoopLocalAsyncLock
from app.core.client_ip import client_ip
from app.core.config import ADMIN_KEY_FILE, settings
from app.core.db import engine
from app.core.redis import redis_client
from app.core.logging_config import request_id_context, trace_id_context

SESSION_PREFIX = "admin:session:"
FAIL_PREFIX = "admin:fail:"
TEMPORARY_KEY_PREFIX = "admin:temporary-key:"
TEMPORARY_KEY_MINUTES = {15, 30, 60, 120}
logger = logging.getLogger("frontiercloud.admin")
_ADMIN_KEY_ROTATION_LOCK = LoopLocalAsyncLock()
ADMIN_KEY_ROTATION_LOCK_KEY = "admin:key:rotation-lock"
ADMIN_KEY_ROTATION_LOCK_SECONDS = 120
ADMIN_KEY_ROTATION_WAIT_SECONDS = 30

_FAILED_ATTEMPT_SCRIPT = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local increment = tonumber(ARGV[2])
local count = tonumber(redis.call('GET', key) or '0') or 0
if increment == 1 then
    count = redis.call('INCR', key)
end
if count > 0 and redis.call('TTL', key) < 0 then
    redis.call('EXPIRE', key, window)
end
return count
"""

_REDEEM_TEMPORARY_KEY_SCRIPT = """
local value = redis.call('GET', KEYS[1])
if not value then
    return false
end
redis.call('DEL', KEYS[1])
return value
"""


@dataclass(frozen=True)
class AdminCredential:
    key_hash: str
    idle_ttl: int
    kind: str


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _failed_attempt_count(redis_key: str, *, increment: bool) -> int:
    return int(await redis_client.eval(
        _FAILED_ATTEMPT_SCRIPT,
        1,
        redis_key,
        settings.ADMIN_FAILED_WINDOW,
        1 if increment else 0,
    ))


async def _store_session(
    redis_key: str,
    mapping: dict[str, str],
    idle_ttl: int = settings.ADMIN_SESSION_TTL,
) -> None:
    pipe = redis_client.pipeline(transaction=True)
    pipe.hset(redis_key, mapping=mapping)
    pipe.expire(redis_key, idle_ttl)
    results = await pipe.execute()
    if len(results) < 2 or not results[-1]:
        raise RedisError("Failed to persist the admin session TTL")


def _fsync_directory(directory: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError:
        # Directory fsync is unavailable on some operating systems. The
        # key file itself has already been flushed and fsynced at this point.
        pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _replace_admin_key_file(new_key: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(ADMIN_KEY_FILE.parent),
        prefix=f".{ADMIN_KEY_FILE.name}.",
        suffix=".new",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(new_key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, ADMIN_KEY_FILE)
        _fsync_directory(ADMIN_KEY_FILE.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


async def _replace_admin_sessions(current_session_key: str, new_key_hash: str) -> None:
    other_session_keys = [
        redis_key
        async for redis_key in redis_client.scan_iter(match=SESSION_PREFIX + "*")
        if redis_key != current_session_key
    ]
    temporary_key_keys = [
        redis_key async for redis_key in redis_client.scan_iter(match=TEMPORARY_KEY_PREFIX + "*")
    ]
    pipe = redis_client.pipeline(transaction=True)
    if other_session_keys:
        pipe.unlink(*other_session_keys)
    if temporary_key_keys:
        pipe.unlink(*temporary_key_keys)
    pipe.hset(current_session_key, mapping={"key_hash": new_key_hash})
    pipe.expire(current_session_key, settings.ADMIN_SESSION_TTL)
    results = await pipe.execute()
    if len(results) < 2 or not results[-1]:
        raise RedisError("Failed to refresh the current admin session TTL")


@asynccontextmanager
async def _admin_key_rotation_guard():
    """Serialize key publication across event loops, workers, and instances."""
    async with _ADMIN_KEY_ROTATION_LOCK:
        distributed_lock = redis_client.lock(
            ADMIN_KEY_ROTATION_LOCK_KEY,
            timeout=ADMIN_KEY_ROTATION_LOCK_SECONDS,
            blocking_timeout=ADMIN_KEY_ROTATION_WAIT_SECONDS,
            thread_local=False,
            raise_on_release_error=False,
        )
        async with distributed_lock:
            yield


def _read_admin_key() -> str:
    try:
        key = ADMIN_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("Admin key was not initialized") from exc
    if not key:
        raise RuntimeError("Admin key is empty")
    return key


def _client_ip(request: Request) -> str:
    return request.scope.get("verified_client_ip") or client_ip(request.scope)


def _ua(request: Request) -> str:
    return request.headers.get("User-Agent", "")[:512]


async def _verify_admin_credential(
    key: str,
    request: Request,
    *,
    allow_temporary: bool,
) -> AdminCredential:
    key = (key or "").strip()
    ip = _client_ip(request)
    fail_key = FAIL_PREFIX + ip
    # Reserve this attempt atomically before the expensive secret comparison.
    # Parallel requests therefore cannot all pass a stale pre-check together.
    failed = await _failed_attempt_count(fail_key, increment=True)
    if failed > settings.ADMIN_MAX_FAILED_ATTEMPTS_PER_IP:
        await audit(None, "admin_login", 0, "", "rate_limited", "", request)
        raise HTTPException(status_code=429, detail={"code": "ADMIN_RATE_LIMITED", "message": "验证请求过于频繁，请稍后再试"})
    supplied_hash = _hash(key) if 1 <= len(key) <= 512 else ""
    persistent_hash = _hash(_read_admin_key())
    if supplied_hash and secrets.compare_digest(supplied_hash, persistent_hash):
        credential = AdminCredential(persistent_hash, settings.ADMIN_SESSION_TTL, "persistent")
    else:
        credential = None
    if credential is None and allow_temporary and supplied_hash:
        serialized = await redis_client.eval(
            _REDEEM_TEMPORARY_KEY_SCRIPT,
            1,
            TEMPORARY_KEY_PREFIX + supplied_hash,
        )
        if serialized:
            try:
                payload = json.loads(serialized)
                idle_ttl = int(payload["idle_ttl"])
                owner_key_hash = str(payload["key_hash"])
                if (
                    idle_ttl in {minutes * 60 for minutes in TEMPORARY_KEY_MINUTES}
                    and secrets.compare_digest(owner_key_hash, persistent_hash)
                ):
                    credential = AdminCredential(persistent_hash, idle_ttl, "temporary")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                credential = None
    if credential is None:
        await audit(None, "admin_login", 0, "", "rejected", "invalid_key", request)
        raise HTTPException(status_code=403, detail={"code": "ADMIN_KEY_INVALID", "message": "Admin Key 无效，请检查输入"})
    await redis_client.delete(fail_key)
    return credential


async def verify_admin_key(key: str, request: Request) -> str:
    credential = await _verify_admin_credential(key, request, allow_temporary=False)
    return credential.key_hash


async def redeem_admin_credential(key: str, request: Request) -> AdminCredential:
    return await _verify_admin_credential(key, request, allow_temporary=True)


async def issue_temporary_admin_key(
    session_hash: str,
    minutes: int,
) -> str:
    if minutes not in TEMPORARY_KEY_MINUTES:
        raise ValueError("临时 Admin Key 有效期必须为 15、30、60 或 120 分钟")
    idle_ttl = minutes * 60
    payload = json.dumps({
        "key_hash": _hash(_read_admin_key()),
        "idle_ttl": idle_ttl,
        "issued_by_session_hash": session_hash,
        "created_at": _now().isoformat(),
    }, separators=(",", ":"))
    for _attempt in range(3):
        temporary_key = secrets.token_urlsafe(32)
        stored = await redis_client.set(
            TEMPORARY_KEY_PREFIX + _hash(temporary_key),
            payload,
            ex=idle_ttl,
            nx=True,
        )
        if stored:
            return temporary_key
    raise RedisError("Failed to allocate a unique temporary Admin Key")


async def create_session(
    key_hash: str,
    request: Request,
    response: Response,
    *,
    idle_ttl: int | None = None,
    credential_kind: str = "persistent",
) -> None:
    idle_ttl = settings.ADMIN_SESSION_TTL if idle_ttl is None else idle_ttl
    session = secrets.token_urlsafe(32)
    session_hash = _hash(session)
    await _store_session(
        SESSION_PREFIX + session_hash,
        {
            "key_hash": key_hash,
            "created_at": _now().isoformat(),
            "idle_ttl": str(idle_ttl),
            "credential_kind": credential_kind,
        },
        idle_ttl,
    )
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(settings.ADMIN_COOKIE_NAME, session, max_age=idle_ttl, httponly=True,
                        secure=settings.ADMIN_COOKIE_SECURE, samesite=settings.ADMIN_COOKIE_SAMESITE, path="/")
    response.set_cookie(settings.ADMIN_CSRF_COOKIE_NAME, csrf, max_age=idle_ttl, httponly=False,
                        secure=settings.ADMIN_COOKIE_SECURE, samesite=settings.ADMIN_COOKIE_SAMESITE, path="/")
    await audit(session_hash, "admin_login", 1, credential_kind, "success", f"idle_ttl={idle_ttl}", request)


async def require_admin(request: Request) -> str:
    session = request.cookies.get(settings.ADMIN_COOKIE_NAME)
    if not session:
        raise HTTPException(status_code=401, detail="特权模式已失效，请重新登录")
    session_hash = _hash(session)
    redis_key = SESSION_PREFIX + session_hash
    data = await redis_client.hgetall(redis_key)
    if not data:
        raise HTTPException(status_code=401, detail="特权模式已失效，请重新登录")
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        csrf_cookie = request.cookies.get(settings.ADMIN_CSRF_COOKIE_NAME)
        csrf_header = request.headers.get("X-CSRF-Token")
        if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
    if not secrets.compare_digest(data.get("key_hash", ""), _hash(_read_admin_key())):
        await redis_client.delete(redis_key)
        raise HTTPException(status_code=401, detail="Admin Key 已变更，请使用新 Key 重新登录")
    credential_kind = data.get("credential_kind", "persistent")
    if credential_kind == "temporary":
        try:
            idle_ttl = int(data.get("idle_ttl", ""))
        except (TypeError, ValueError):
            idle_ttl = 0
        if idle_ttl not in {minutes * 60 for minutes in TEMPORARY_KEY_MINUTES}:
            await redis_client.delete(redis_key)
            raise HTTPException(status_code=401, detail="临时特权会话无效，请重新登录")
    else:
        credential_kind = "persistent"
        idle_ttl = settings.ADMIN_SESSION_TTL
    if not await redis_client.expire(redis_key, idle_ttl):
        raise HTTPException(status_code=401, detail="特权模式已失效，请重新登录")
    request.scope["admin_authenticated"] = True
    request.scope["admin_session_cookie"] = session
    request.scope["admin_session_ttl"] = idle_ttl
    request.scope["admin_credential_kind"] = credential_kind
    return session_hash


async def rotate_admin_key(session_hash: str, custom_key: str | None, confirmation: str | None) -> str:
    if custom_key is None:
        new_key = secrets.token_urlsafe(48)
    else:
        new_key = custom_key.strip()
        if len(new_key) < 16 or len(new_key) > 512:
            raise ValueError("自定义 Admin Key 长度必须为 16 到 512 个字符")
        if not confirmation or not secrets.compare_digest(new_key, confirmation):
            raise ValueError("两次输入的 Admin Key 不一致")
    async with _admin_key_rotation_guard():
        if secrets.compare_digest(_hash(new_key), _hash(_read_admin_key())):
            raise ValueError("新 Admin Key 不能与当前 Key 相同")
        _replace_admin_key_file(new_key)
        try:
            await _replace_admin_sessions(SESSION_PREFIX + session_hash, _hash(new_key))
        except RedisError:
            # The file is the source of truth. Do not hide a newly published
            # random key from the administrator merely because session cache
            # reconciliation failed; all stale sessions fail the file-hash
            # check in require_admin and the returned key can re-authenticate.
            logger.exception(
                "admin_key_session_reconciliation_failed",
                extra={"context": {"session_hash": session_hash}},
            )
        logger.warning("admin_key_rotated", extra={"context": {"session_hash": session_hash}})
        return new_key


async def logout_admin(request: Request, response: Response) -> None:
    request.scope["admin_authenticated"] = False
    session = request.cookies.get(settings.ADMIN_COOKIE_NAME)
    if session:
        await redis_client.delete(SESSION_PREFIX + _hash(session))
        await audit(_hash(session), "admin_logout", 1, "", "success", "", request)
    response.delete_cookie(settings.ADMIN_COOKIE_NAME, path="/")
    response.delete_cookie(settings.ADMIN_CSRF_COOKIE_NAME, path="/")


async def audit(session_hash: Optional[str], action: str, target_count: int, source_summary: str,
                result: str, detail: str, request: Request,
                *, conn: AsyncConnection | None = None) -> None:
    """Passed connections preserve atomicity; post-action failures retain evidence."""
    values = {"sid": session_hash, "action": action[:64], "count": target_count,
              "summary": source_summary[:10000], "result": result[:32], "detail": detail[:10000],
              "ip": _client_ip(request), "ua": _ua(request), "created": _now(),
              "request_id": request.scope.get("request_id") or request_id_context.get() or None,
              "trace_id": request.scope.get("trace_id") or trace_id_context.get() or None}
    statement = text("""
        INSERT INTO admin_audit_log
        (session_id_hash, action, target_count, source_summary, result, detail,
         client_ip, user_agent, created_at, request_id, trace_id)
        VALUES (:sid, :action, :count, :summary, :result, :detail,
                :ip, :ua, :created, :request_id, :trace_id)
    """)
    if conn is not None:
        await conn.execute(statement, values)
        return
    try:
        async with engine.begin() as audit_conn:
            await audit_conn.execute(statement, values)
    except SQLAlchemyError as exc:
        # Audit is a side channel for filesystem and Redis actions. Do not
        # report an already committed action as failed solely because the
        # separate audit insert was unavailable.
        logger.exception(
            "admin_audit_write_failed",
            extra={"context": {
                "audit_evidence": values, "error": str(exc), "durable_audit": False,
            }},
        )
