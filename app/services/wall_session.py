from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response

from app.core.client_ip import client_ip
from app.core.config import settings
from app.core.redis import redis_client


SESSION_COOKIE = "__Host-wall_session"
SESSION_PREFIX = "wall:session:"
RATE_PREFIX = "wall:rate:"

AVATARS = (
    {"id": "cloud", "symbol": "☁", "label": "云朵"},
    {"id": "penguin", "symbol": "●", "label": "企鹅"},
    {"id": "moon", "symbol": "☾", "label": "月亮"},
    {"id": "star", "symbol": "✦", "label": "星星"},
    {"id": "leaf", "symbol": "◆", "label": "叶片"},
    {"id": "wave", "symbol": "≈", "label": "海浪"},
    {"id": "snow", "symbol": "❄", "label": "雪花"},
    {"id": "planet", "symbol": "◉", "label": "行星"},
)
AVATAR_IDS = frozenset(item["id"] for item in AVATARS)


def _hash_token(value: str) -> str:
    return hmac.new(
        settings.WALL_ADMIN_TOKEN.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _verified_ip(request: Request) -> str:
    return request.scope.get("verified_client_ip") or client_ip(request.scope)


@dataclass(frozen=True)
class WallSession:
    session_hash: str
    avatar_id: str
    csrf_token: str
    expires_at: str


class WallSessionService:
    async def create(self, avatar_id: str, response: Response) -> WallSession:
        if avatar_id not in AVATAR_IDS:
            raise ValueError("Invalid avatar")
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        session_hash = _hash_token(token)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.WALL_SESSION_TTL)
        payload = {
            "avatar_id": avatar_id,
            "csrf_token": csrf_token,
            "expires_at": expires_at.isoformat(),
        }
        await redis_client.set(
            f"{SESSION_PREFIX}{session_hash}",
            json.dumps(payload),
            ex=settings.WALL_SESSION_TTL,
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.WALL_SESSION_TTL,
            secure=settings.ADMIN_COOKIE_SECURE,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return WallSession(session_hash, avatar_id, csrf_token, expires_at.isoformat())

    async def current(self, request: Request) -> WallSession | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        session_hash = _hash_token(token)
        raw = await redis_client.get(f"{SESSION_PREFIX}{session_hash}")
        if not raw:
            return None
        payload = json.loads(raw)
        avatar_id = str(payload.get("avatar_id", ""))
        csrf_token = str(payload.get("csrf_token", ""))
        expires_at = str(payload.get("expires_at", ""))
        if avatar_id not in AVATAR_IDS or not csrf_token or not expires_at:
            await redis_client.delete(f"{SESSION_PREFIX}{session_hash}")
            return None
        return WallSession(session_hash, avatar_id, csrf_token, expires_at)

    async def require(self, request: Request, csrf_token: str | None = None) -> WallSession:
        session = await self.current(request)
        if not session:
            raise HTTPException(status_code=401, detail="匿名会话已过期，请重新选择头像")
        if csrf_token is not None and not secrets.compare_digest(csrf_token, session.csrf_token):
            raise HTTPException(status_code=403, detail="页面凭据无效，请刷新后重试")
        return session

    async def allow_action(self, request: Request, action: str, cooldown: int) -> bool:
        ip_digest = _hash_token(_verified_ip(request))
        key = f"{RATE_PREFIX}{action}:{ip_digest}"
        return bool(await redis_client.set(key, "1", ex=cooldown, nx=True))

    async def destroy(self, request: Request, response: Response) -> None:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            await redis_client.delete(f"{SESSION_PREFIX}{_hash_token(token)}")
        response.delete_cookie(SESSION_COOKIE, path="/")


wall_sessions = WallSessionService()
