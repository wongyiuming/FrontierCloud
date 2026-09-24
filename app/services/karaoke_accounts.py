"""Master-owned Karaoke accounts, sessions, quotas, and durable audit."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
import unicodedata
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request, Response
from redis.exceptions import RedisError
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError

from app.core.client_ip import resolve_client_identity
from app.core.config import settings
from app.core.redis import redis_client
from app.services import karaoke_schema as ks
from app.services.federation import protocol as p
from app.services.federation import schema as fs
from app.services.federation.state import state
from app.services.network_observation import normalize_observed_addresses

SESSION_PREFIX = "karaoke:session:"
LOGIN_FAILURE_PREFIX = "karaoke:login-fail:"
REGISTER_FAILURE_PREFIX = "karaoke:register-fail:"
REGISTER_SUCCESS_PREFIX = "karaoke:register-success:"
CAPTCHA_PREFIX = "karaoke:captcha:"
SESSION_TTL = 7 * 86400
CAPTCHA_TTL = 300
DEFAULT_QUOTA_BYTES = 200 * 1024 * 1024
MAX_QUOTA_BYTES = 10 * 1024 * 1024 * 1024 * 1024
USERNAME = re.compile(r"^[\w\u3400-\u9fff]{3,32}$", re.UNICODE)
CAPTCHA_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def require_master() -> None:
    if state.node.get("role") != "Master":
        raise HTTPException(409, "账号与录音管理仅由 Master 提供")


def normalize_username(value: str) -> tuple[str, str]:
    name = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not USERNAME.fullmatch(name):
        raise ValueError("用户名需为 3–32 位中文、字母、数字或下划线")
    return name, name.casefold()


def validate_password(value: str) -> None:
    if not 10 <= len(value) <= 128:
        raise ValueError("密码需为 10–128 个字符")
    checks = (re.search(r"[A-Z]", value), re.search(r"[a-z]", value),
              re.search(r"\d", value), re.search(r"[^A-Za-z0-9]", value))
    if not all(checks):
        raise ValueError("密码必须同时包含大写字母、小写字母、数字和特殊字符，例如 Huawei@123")


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${p.encode(salt)}${p.encode(digest)}"


DUMMY_PASSWORD_HASH = _password_hash("FrontierCloud@Invalid1", b"\0" * 16)


async def hash_password(password: str) -> str:
    validate_password(password)
    return await asyncio.to_thread(_password_hash, password)


async def verify_password(password: str, encoded: str) -> bool:
    try:
        kind, n, r, parallel, salt, expected = encoded.split("$")
        if kind != "scrypt" or (int(n), int(r), int(parallel)) != (16384, 8, 1):
            return False
        actual = await asyncio.to_thread(
            hashlib.scrypt, password.encode("utf-8"), salt=p.decode(salt), n=int(n), r=int(r), p=int(parallel), dklen=32,
        )
        return hmac.compare_digest(actual, p.decode(expected))
    except (ValueError, TypeError, p.ProtocolError):
        return False


def cookie_names() -> tuple[str, str]:
    prefix = "__Host-" if settings.TLS_ENABLED else ""
    return prefix + "karaoke_session", prefix + "karaoke_csrf"


def _set_session_cookies(response: Response, session: str, csrf: str) -> None:
    session_name, csrf_name = cookie_names()
    response.set_cookie(session_name, session, max_age=SESSION_TTL, httponly=True,
                        secure=settings.TLS_ENABLED, samesite="lax", path="/")
    response.set_cookie(csrf_name, csrf, max_age=SESSION_TTL, httponly=False,
                        secure=settings.TLS_ENABLED, samesite="lax", path="/")


def clear_session_cookies(response: Response) -> None:
    for name in cookie_names():
        response.delete_cookie(name, secure=settings.TLS_ENABLED, samesite="lax", path="/")


async def create_session(user: dict, response: Response) -> None:
    session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    await redis_client.hset(SESSION_PREFIX + hashlib.sha256(session.encode()).hexdigest(), mapping={
        "user_id": user["user_id"], "csrf": csrf,
    })
    await redis_client.expire(SESSION_PREFIX + hashlib.sha256(session.encode()).hexdigest(), SESSION_TTL)
    _set_session_cookies(response, session, csrf)


async def current_user(request: Request, *, mutation: bool = False, optional: bool = False) -> dict | None:
    session_name, csrf_name = cookie_names()
    raw = request.cookies.get(session_name)
    if not raw:
        if optional:
            return None
        raise HTTPException(401, "请先登录 K歌账号")
    key = SESSION_PREFIX + hashlib.sha256(raw.encode()).hexdigest()
    values = await redis_client.hgetall(key)
    if not values:
        if optional:
            return None
        raise HTTPException(401, "K歌登录已失效")
    if mutation:
        supplied = request.headers.get("x-karaoke-csrf", "")
        cookie = request.cookies.get(csrf_name, "")
        if not cookie or not supplied or not secrets.compare_digest(cookie, supplied) or not secrets.compare_digest(cookie, values.get("csrf", "")):
            raise HTTPException(403, "K歌请求校验失败")
    async with state.database.connect() as conn:
        row = (await conn.execute(select(ks.users).where(ks.users.c.user_id == values["user_id"]))).mappings().first()
    if not row or row["status"] != "active":
        await redis_client.delete(key)
        if optional:
            return None
        raise HTTPException(403, "账号已被封禁或删除")
    await redis_client.expire(key, SESSION_TTL)
    request.scope["karaoke_user_id"] = row["user_id"]
    return dict(row)


async def invalidate_user_sessions(user_id: str) -> None:
    keys = []
    async for key in redis_client.scan_iter(match=SESSION_PREFIX + "*"):
        if await redis_client.hget(key, "user_id") == user_id:
            keys.append(key)
    if keys:
        await redis_client.unlink(*keys)


def _audit_values(request: Request, user_id: str | None, action: str, result: str,
                  addresses: list[str] | None, detail: dict) -> dict:
    return {
        "user_id": user_id, "action": action, "result": result,
        "client_ip": resolve_client_identity(request.scope).ip,
        "webrtc_addresses": list(addresses or []), "detail": detail,
        "request_id": request.scope.get("request_id"), "trace_id": request.scope.get("trace_id"),
        "created_at": int(time.time()),
    }


async def audit(request: Request, user_id: str | None, action: str, result: str,
                addresses: list[str] | None = None, detail: dict | None = None, conn=None) -> None:
    values = _audit_values(request, user_id, action, result, addresses, detail or {})
    if conn is not None:
        await conn.execute(insert(ks.audit).values(**values))
    else:
        async with state.database.begin() as transaction:
            await transaction.execute(insert(ks.audit).values(**values))


def _day_key(prefix: str, ip: str) -> str:
    day = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    return f"{prefix}{day}:{ip}"


async def _increment_daily(key: str) -> int:
    pipe = redis_client.pipeline(transaction=True)
    pipe.incr(key)
    pipe.expire(key, 172800)
    result = await pipe.execute()
    return int(result[0])


def _registration_day() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


async def _registration_counts(ip: str) -> tuple[int, int]:
    async with state.database.connect() as conn:
        row = (await conn.execute(select(ks.registration_daily).where(
            ks.registration_daily.c.client_ip == ip,
            ks.registration_daily.c.day_key == _registration_day(),
        ))).mappings().first()
    return (int(row["failure_count"]), int(row["success_count"])) if row else (0, 0)


async def _record_registration_failure(ip: str) -> int:
    now, day = int(time.time()), _registration_day()
    async with state.database.begin() as conn:
        await conn.execute(mysql_insert(ks.registration_daily).values(
            client_ip=ip, day_key=day, failure_count=0, success_count=0, updated_at=now,
        ).prefix_with("IGNORE"))
        row = (await conn.execute(select(ks.registration_daily).where(
            ks.registration_daily.c.client_ip == ip, ks.registration_daily.c.day_key == day,
        ).with_for_update())).mappings().one()
        if int(row["failure_count"]) >= 20:
            return int(row["failure_count"])
        count = int(row["failure_count"]) + 1
        await conn.execute(update(ks.registration_daily).where(
            ks.registration_daily.c.client_ip == ip, ks.registration_daily.c.day_key == day,
        ).values(failure_count=count, updated_at=now))
        return count


def normalize_webrtc(values: list[str] | None) -> list[str]:
    return normalize_observed_addresses(values or [])


async def captcha_create() -> tuple[str, str]:
    challenge = uuid.uuid4().hex
    answer = "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(5))
    pipe = redis_client.pipeline(transaction=True)
    pipe.set(CAPTCHA_PREFIX + challenge, hashlib.sha256(answer.encode()).hexdigest(), ex=CAPTCHA_TTL)
    pipe.set(CAPTCHA_PREFIX + challenge + ":image", answer, ex=CAPTCHA_TTL)
    await pipe.execute()
    return challenge, answer


async def captcha_consume(challenge: str | None, answer: str | None) -> bool:
    if not challenge or not answer or not p.IDENTIFIER.fullmatch(challenge):
        return False
    key = CAPTCHA_PREFIX + challenge
    stored = await redis_client.getdel(key)
    await redis_client.delete(key + ":image")
    supplied = hashlib.sha256(str(answer).strip().upper().encode()).hexdigest()
    return bool(stored and hmac.compare_digest(stored, supplied))


def captcha_svg(answer: str) -> str:
    letters = "".join(
        f'<text x="{18 + index * 27}" y="40" transform="rotate({(-8 + index * 4)} {18 + index * 27} 40)">{letter}</text>'
        for index, letter in enumerate(answer)
    )
    return ("<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"160\" height=\"56\" viewBox=\"0 0 160 56\">"
            "<rect width=\"160\" height=\"56\" rx=\"10\" fill=\"#101827\"/>"
            "<path d=\"M4 43L156 13M8 16L151 47\" stroke=\"#44617d\" stroke-width=\"2\"/>"
            f"<g fill=\"#8be3ff\" font-family=\"monospace\" font-size=\"28\" font-weight=\"700\">{letters}</g></svg>")


async def register(request: Request, response: Response, username: str, password: str,
                   challenge: str | None, captcha: str | None, addresses: list[str] | None) -> dict:
    require_master()
    ip = resolve_client_identity(request.scope).ip
    addresses = normalize_webrtc(addresses)
    failures, successes = await _registration_counts(ip)
    if failures >= 20:
        raise HTTPException(429, "该公网 IP 今日注册失败次数已达上限")
    if successes >= 3:
        raise HTTPException(429, "该公网 IP 今日成功注册次数已达上限")
    try:
        if not await captcha_consume(challenge, captcha):
            raise ValueError("验证码无效或已过期")
        name, key = normalize_username(username)
        encoded = await hash_password(password)
        now, user_id = int(time.time()), uuid.uuid4().hex
        async with state.database.begin() as conn:
            day = _registration_day()
            await conn.execute(mysql_insert(ks.registration_daily).values(
                client_ip=ip, day_key=day, failure_count=0, success_count=0, updated_at=now,
            ).prefix_with("IGNORE"))
            daily = (await conn.execute(select(ks.registration_daily).where(
                ks.registration_daily.c.client_ip == ip, ks.registration_daily.c.day_key == day,
            ).with_for_update())).mappings().one()
            if int(daily["failure_count"]) >= 20:
                raise HTTPException(429, "该公网 IP 今日注册失败次数已达上限")
            if int(daily["success_count"]) >= 3:
                raise HTTPException(429, "该公网 IP 今日成功注册次数已达上限")
            exists = await conn.scalar(select(ks.users.c.user_id).where(ks.users.c.username_key == key))
            if exists:
                raise ValueError("用户名已存在")
            await conn.execute(insert(ks.users).values(
                user_id=user_id, username=name, username_key=key, password_hash=encoded,
                status="active", quota_bytes=DEFAULT_QUOTA_BYTES, used_bytes=0,
                created_at=now, updated_at=now,
            ))
            await audit(request, user_id, "register", "success", addresses, {}, conn)
            await conn.execute(update(ks.registration_daily).where(
                ks.registration_daily.c.client_ip == ip, ks.registration_daily.c.day_key == day,
            ).values(success_count=ks.registration_daily.c.success_count + 1, updated_at=now))
        user = {"user_id": user_id, "username": name}
        await create_session(user, response)
        return public_user({**user, "status": "active", "quota_bytes": DEFAULT_QUOTA_BYTES,
                            "used_bytes": 0})
    except (ValueError, RedisError, IntegrityError) as exc:
        await _record_registration_failure(ip)
        await audit(request, None, "register", "failure", addresses, {"reason": str(exc)[:200]})
        detail = "用户名已存在" if isinstance(exc, IntegrityError) else str(exc)
        raise HTTPException(400, detail) from exc


async def login(request: Request, response: Response, username: str, password: str,
                challenge: str | None, captcha: str | None, addresses: list[str] | None) -> dict:
    require_master()
    addresses = normalize_webrtc(addresses)
    ip = resolve_client_identity(request.scope).ip
    try:
        _name, key = normalize_username(username)
    except ValueError:
        key = unicodedata.normalize("NFKC", str(username or "")).casefold()[:128]
    failure_key = LOGIN_FAILURE_PREFIX + hashlib.sha256(f"{ip}:{key}".encode()).hexdigest()
    failures = int(await redis_client.get(failure_key) or 0)
    if failures >= 3 and not await captcha_consume(challenge, captcha):
        raise HTTPException(400, "第 4 次登录起必须完成验证码", headers={"X-Captcha-Required": "1"})
    async with state.database.connect() as conn:
        row = (await conn.execute(select(ks.users).where(ks.users.c.username_key == key))).mappings().first()
    verified = await verify_password(password, row["password_hash"] if row else DUMMY_PASSWORD_HASH)
    verified = bool(row and verified)
    if not verified:
        count = await redis_client.incr(failure_key)
        await redis_client.expire(failure_key, 86400)
        await audit(request, row["user_id"] if row else None, "login", "failure", addresses, {"failures": int(count)})
        raise HTTPException(401, "用户名或密码错误", headers={"X-Captcha-Required": "1" if int(count) >= 3 else "0"})
    if row["status"] != "active":
        await audit(request, row["user_id"], "login", "blocked", addresses)
        raise HTTPException(403, "账号已被封禁")
    await redis_client.delete(failure_key)
    await create_session(dict(row), response)
    await audit(request, row["user_id"], "login", "success", addresses)
    return public_user(dict(row))


def public_user(row: dict) -> dict:
    return {key: row.get(key) for key in (
        "user_id", "username", "status", "quota_bytes", "used_bytes"
    )}


async def available_storage_nodes() -> list[dict]:
    if state.node.get("role") != "Master":
        return []
    from app.services import resource_pool
    return [{"member_id": row["member_id"], "name": "Master Local" if row["member_kind"] == "MasterLocal" else row["member_id"],
             "mode": row["transport"], "used_bytes": row["used_bytes"],
             "capacity_bytes": row["allocated_bytes"], "available_bytes": row["available_bytes"]}
            for row in await resource_pool.list_members(state.database)
            if row["storage_enabled"] and row["health"] == "online" and row["writable"]]
