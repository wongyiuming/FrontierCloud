import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request, Response
from sqlalchemy import text

from app.core.admin_log import append_admin_log
from app.core.config import settings
from app.core.db import engine
from app.core.redis import redis_client

TOKEN_PREFIX = "admin:token:"
SESSION_PREFIX = "admin:session:"
FAIL_PREFIX = "admin:fail:"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _client_ip(request: Request) -> str:
    # Nginx overwrites these headers; never trust arbitrary proxy chains here.
    return request.headers.get("X-Real-IP") or (request.client.host if request.client else "127.0.0.1")


def _ua(request: Request) -> str:
    return request.headers.get("User-Agent", "")[:512]


async def issue_admin_token() -> str:
    token = secrets.token_urlsafe(32)
    token_hash = _hash(token)
    expires_at = _now() + timedelta(seconds=settings.ADMIN_TOKEN_INITIAL_TTL)
    await redis_client.set(
        TOKEN_PREFIX + token_hash,
        "pending",
        ex=settings.ADMIN_TOKEN_INITIAL_TTL,
    )
    async with engine.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO admin_token_history
                (token_hash, created_at, expires_at, status)
                VALUES (:token_hash, :created_at, :expires_at, 'active')
            """),
            {"token_hash": token_hash, "created_at": _now(), "expires_at": expires_at},
        )
    log_line = (
        f"[ADMIN_TOKEN] temporary admin token={token} "
        f"created_at={_now().isoformat()} claim_expires_at={expires_at.isoformat()}"
    )
    print(log_line, flush=True)
    append_admin_log(log_line)
    return token


async def verify_admin_token(token: str, request: Request) -> str:
    token = (token or "").strip()
    if not token or len(token) > 256:
        raise HTTPException(status_code=403, detail="特权验证失败")

    ip = _client_ip(request)
    fail_key = FAIL_PREFIX + ip
    failed = await redis_client.get(fail_key)
    if failed and int(failed) >= settings.ADMIN_MAX_FAILED_ATTEMPTS_PER_IP:
        raise HTTPException(status_code=429, detail="验证请求过于频繁，请稍后再试")

    token_hash = _hash(token)
    key = TOKEN_PREFIX + token_hash
    token_state = await redis_client.get(key)
    if not token_state:
        count = await redis_client.incr(fail_key)
        if count == 1:
            await redis_client.expire(fail_key, settings.ADMIN_FAILED_WINDOW)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE admin_token_history SET failed_attempts = failed_attempts + 1 WHERE token_hash=:h"),
                {"h": token_hash},
            )
        raise HTTPException(status_code=403, detail="特权验证失败")

    # An unused token has a longer, finite claim window. Its first successful
    # use activates the normal short sliding TTL used by admin sessions.
    await redis_client.set(key, "active", ex=settings.ADMIN_TOKEN_TTL)
    await redis_client.delete(fail_key)
    async with engine.begin() as conn:
        await conn.execute(
            text("""
                UPDATE admin_token_history
                SET first_used_at = COALESCE(first_used_at, :now),
                    last_used_at = :now,
                    expires_at = :expires_at,
                    use_count = use_count + 1,
                    last_ip = :ip,
                    last_user_agent = :ua,
                    status = 'active'
                WHERE token_hash = :h
            """),
            {
                "h": token_hash,
                "now": _now(),
                "expires_at": _now() + timedelta(seconds=settings.ADMIN_TOKEN_TTL),
                "ip": ip,
                "ua": _ua(request),
            },
        )
    return token_hash


async def create_session(token_hash: str, request: Request, response: Response) -> None:
    session = secrets.token_urlsafe(32)
    session_hash = _hash(session)
    await redis_client.hset(
        SESSION_PREFIX + session_hash,
        mapping={"token_hash": token_hash, "created_at": _now().isoformat()},
    )
    await redis_client.expire(SESSION_PREFIX + session_hash, settings.ADMIN_SESSION_TTL)

    csrf = secrets.token_urlsafe(32)
    response.set_cookie(
        settings.ADMIN_COOKIE_NAME,
        session,
        max_age=settings.ADMIN_SESSION_TTL,
        httponly=True,
        secure=settings.ADMIN_COOKIE_SECURE,
        samesite=settings.ADMIN_COOKIE_SAMESITE,
        path="/",
    )
    response.set_cookie(
        settings.ADMIN_CSRF_COOKIE_NAME,
        csrf,
        max_age=settings.ADMIN_SESSION_TTL,
        httponly=False,
        secure=settings.ADMIN_COOKIE_SECURE,
        samesite=settings.ADMIN_COOKIE_SAMESITE,
        path="/",
    )


async def require_admin(request: Request) -> str:
    session = request.cookies.get(settings.ADMIN_COOKIE_NAME)
    if not session:
        raise HTTPException(status_code=401, detail="特权模式已失效，请重新提权")

    session_hash = _hash(session)
    key = SESSION_PREFIX + session_hash
    data = await redis_client.hgetall(key)
    if not data:
        raise HTTPException(status_code=401, detail="特权模式已失效，请重新提权")

    csrf_cookie = request.cookies.get(settings.ADMIN_CSRF_COOKIE_NAME)
    csrf_header = request.headers.get("X-CSRF-Token")
    method = request.method.upper()
    if method in {"POST", "PUT", "PATCH", "DELETE"} and (
        not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header)
    ):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")

    token_hash = data.get("token_hash")
    if not token_hash or not await redis_client.exists(TOKEN_PREFIX + token_hash):
        await redis_client.delete(key)
        raise HTTPException(status_code=401, detail="特权凭证已过期，请重新提权")

    await redis_client.expire(key, settings.ADMIN_SESSION_TTL)
    await redis_client.expire(TOKEN_PREFIX + token_hash, settings.ADMIN_TOKEN_TTL)
    return session_hash


async def logout_admin(request: Request, response: Response) -> None:
    session = request.cookies.get(settings.ADMIN_COOKIE_NAME)
    if session:
        await redis_client.delete(SESSION_PREFIX + _hash(session))
    response.delete_cookie(settings.ADMIN_COOKIE_NAME, path="/")
    response.delete_cookie(settings.ADMIN_CSRF_COOKIE_NAME, path="/")


async def audit(session_hash: Optional[str], action: str, target_count: int, source_summary: str,
                result: str, detail: str, request: Request) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO admin_audit_log
                (session_id_hash, action, target_count, source_summary, result, detail, client_ip, user_agent, created_at)
                VALUES (:sid, :action, :count, :summary, :result, :detail, :ip, :ua, :created)
            """),
            {
                "sid": session_hash,
                "action": action[:64],
                "count": target_count,
                "summary": source_summary[:10000],
                "result": result[:32],
                "detail": detail[:10000],
                "ip": _client_ip(request),
                "ua": _ua(request),
                "created": _now(),
            },
        )
