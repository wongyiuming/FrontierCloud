from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, Request, Response
from sqlalchemy import text

from app.core.client_ip import client_ip
from app.core.config import ADMIN_KEY_FILE, settings
from app.core.db import engine
from app.core.redis import redis_client

SESSION_PREFIX = "admin:session:"
FAIL_PREFIX = "admin:fail:"
logger = logging.getLogger("frontiercloud.admin")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    failed = await redis_client.get(fail_key)
    if failed and int(failed) >= settings.ADMIN_MAX_FAILED_ATTEMPTS_PER_IP:
        raise HTTPException(status_code=429, detail={"code": "ADMIN_RATE_LIMITED", "message": "验证请求过于频繁，请稍后再试"})
    valid = 1 <= len(key) <= 512 and secrets.compare_digest(_hash(key), _hash(_read_admin_key()))
    if not valid:
        count = await redis_client.incr(fail_key)
        if count == 1:
            await redis_client.expire(fail_key, settings.ADMIN_FAILED_WINDOW)
        raise HTTPException(status_code=403, detail={"code": "ADMIN_KEY_INVALID", "message": "Admin Key 无效，请检查输入"})
    await redis_client.delete(fail_key)
    return _hash(key)


async def create_session(key_hash: str, request: Request, response: Response) -> None:
    session = secrets.token_urlsafe(32)
    session_hash = _hash(session)
    await redis_client.hset(SESSION_PREFIX + session_hash, mapping={"key_hash": key_hash, "created_at": _now().isoformat()})
    await redis_client.expire(SESSION_PREFIX + session_hash, settings.ADMIN_SESSION_TTL)
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
    if secrets.compare_digest(_hash(new_key), _hash(_read_admin_key())):
        raise ValueError("新 Admin Key 不能与当前 Key 相同")
    temporary = ADMIN_KEY_FILE.with_suffix(".new")
    temporary.write_text(new_key + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, ADMIN_KEY_FILE)
    current_session_key = SESSION_PREFIX + session_hash
    async for redis_key in redis_client.scan_iter(match=SESSION_PREFIX + "*"):
        if redis_key != current_session_key:
            await redis_client.delete(redis_key)
    await redis_client.hset(current_session_key, mapping={"key_hash": _hash(new_key)})
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
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO admin_audit_log
            (session_id_hash, action, target_count, source_summary, result, detail, client_ip, user_agent, created_at)
            VALUES (:sid, :action, :count, :summary, :result, :detail, :ip, :ua, :created)
        """), {"sid": session_hash, "action": action[:64], "count": target_count,
                 "summary": source_summary[:10000], "result": result[:32], "detail": detail[:10000],
                 "ip": _client_ip(request), "ua": _ua(request), "created": _now()})
