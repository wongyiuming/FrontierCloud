from __future__ import annotations

import hashlib
import logging
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request, Response
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.async_lock import LoopLocalAsyncLock
from app.core.client_ip import client_ip
from app.core.config import ADMIN_KEY_FILE, settings
from app.core.db import engine
from app.core.redis import redis_client

SESSION_PREFIX = "admin:session:"
FAIL_PREFIX = "admin:fail:"
logger = logging.getLogger("frontiercloud.admin")
_ADMIN_KEY_ROTATION_LOCK = LoopLocalAsyncLock()

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


async def _store_session(redis_key: str, mapping: dict[str, str]) -> None:
    pipe = redis_client.pipeline(transaction=True)
    pipe.hset(redis_key, mapping=mapping)
    pipe.expire(redis_key, settings.ADMIN_SESSION_TTL)
    results = await pipe.execute()
    if len(results) < 2 or not results[-1]:
        raise RedisError("Failed to persist the admin session TTL")


def _fsync_directory(directory: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError:
        # Directory fsync is unavailable on some development platforms. The
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
    pipe = redis_client.pipeline(transaction=True)
    if other_session_keys:
        pipe.unlink(*other_session_keys)
    pipe.hset(current_session_key, mapping={"key_hash": new_key_hash})
    pipe.expire(current_session_key, settings.ADMIN_SESSION_TTL)
    results = await pipe.execute()
    if len(results) < 2 or not results[-1]:
        raise RedisError("Failed to refresh the current admin session TTL")


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


async def verify_admin_key(key: str, request: Request) -> str:
    key = (key or "").strip()
    ip = _client_ip(request)
    fail_key = FAIL_PREFIX + ip
    failed = await _failed_attempt_count(fail_key, increment=False)
    if failed >= settings.ADMIN_MAX_FAILED_ATTEMPTS_PER_IP:
        raise HTTPException(status_code=429, detail={"code": "ADMIN_RATE_LIMITED", "message": "验证请求过于频繁，请稍后再试"})
    valid = 1 <= len(key) <= 512 and secrets.compare_digest(_hash(key), _hash(_read_admin_key()))
    if not valid:
        await _failed_attempt_count(fail_key, increment=True)
        raise HTTPException(status_code=403, detail={"code": "ADMIN_KEY_INVALID", "message": "Admin Key 无效，请检查输入"})
    await redis_client.delete(fail_key)
    return _hash(key)


async def create_session(key_hash: str, request: Request, response: Response) -> None:
    session = secrets.token_urlsafe(32)
    session_hash = _hash(session)
    await _store_session(
        SESSION_PREFIX + session_hash,
        {"key_hash": key_hash, "created_at": _now().isoformat()},
    )
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(settings.ADMIN_COOKIE_NAME, session, max_age=settings.ADMIN_SESSION_TTL, httponly=True,
                        secure=settings.ADMIN_COOKIE_SECURE, samesite=settings.ADMIN_COOKIE_SAMESITE, path="/")
    response.set_cookie(settings.ADMIN_CSRF_COOKIE_NAME, csrf, max_age=settings.ADMIN_SESSION_TTL, httponly=False,
                        secure=settings.ADMIN_COOKIE_SECURE, samesite=settings.ADMIN_COOKIE_SAMESITE, path="/")


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
    await redis_client.expire(redis_key, settings.ADMIN_SESSION_TTL)
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
    async with _ADMIN_KEY_ROTATION_LOCK:
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
    session = request.cookies.get(settings.ADMIN_COOKIE_NAME)
    if session:
        await redis_client.delete(SESSION_PREFIX + _hash(session))
    response.delete_cookie(settings.ADMIN_COOKIE_NAME, path="/")
    response.delete_cookie(settings.ADMIN_CSRF_COOKIE_NAME, path="/")


async def audit(session_hash: Optional[str], action: str, target_count: int, source_summary: str,
                result: str, detail: str, request: Request) -> None:
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO admin_audit_log
                (session_id_hash, action, target_count, source_summary, result, detail, client_ip, user_agent, created_at)
                VALUES (:sid, :action, :count, :summary, :result, :detail, :ip, :ua, :created)
            """), {"sid": session_hash, "action": action[:64], "count": target_count,
                     "summary": source_summary[:10000], "result": result[:32], "detail": detail[:10000],
                     "ip": _client_ip(request), "ua": _ua(request), "created": _now()})
    except SQLAlchemyError as exc:
        # Audit is a side channel for filesystem and Redis actions. Do not
        # report an already committed action as failed solely because the
        # separate audit insert was unavailable.
        logger.exception(
            "admin_audit_write_failed",
            extra={"context": {"action": action[:64], "error": str(exc)}},
        )
