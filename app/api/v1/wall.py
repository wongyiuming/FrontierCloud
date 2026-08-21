from __future__ import annotations

import os

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.config import settings
from app.services.wall_session import AVATARS, wall_sessions
from app.services.wall_store import TEXT_CIPHERTEXT_LIMIT, wall_store


router = APIRouter()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
STATIC_PATH = os.path.join(BASE_DIR, "static", "wall", "index.html")


class AvatarSelection(BaseModel):
    avatar_id: str


@router.get("", include_in_schema=False)
async def read_wall_index():
    return FileResponse(STATIC_PATH, headers={"Cache-Control": "no-store"})


@router.get("/avatars")
async def list_avatars():
    return {"avatars": AVATARS, "session_ttl_seconds": settings.WALL_SESSION_TTL}


@router.post("/session")
async def create_session(selection: AvatarSelection, response: Response):
    try:
        session = await wall_sessions.create(selection.avatar_id, response)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "avatar_id": session.avatar_id,
        "csrf_token": session.csrf_token,
        "expires_at": session.expires_at,
    }


@router.get("/session")
async def current_session(request: Request):
    session = await wall_sessions.current(request)
    if not session:
        raise HTTPException(status_code=401, detail="匿名会话不存在或已过期")
    return {
        "avatar_id": session.avatar_id,
        "csrf_token": session.csrf_token,
        "expires_at": session.expires_at,
    }


@router.delete("/session", status_code=204)
async def leave_session(request: Request, response: Response):
    await wall_sessions.destroy(request, response)


@router.get("/messages")
async def list_messages(request: Request):
    await wall_sessions.require(request)
    return {"messages": await wall_store.list_messages(), "ttl_seconds": settings.WALL_TTL}


async def _read_encrypted_payload(upload: UploadFile, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise HTTPException(status_code=413, detail="加密内容超过大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/messages", status_code=201)
async def publish_message(
    request: Request,
    kind: str = Form(...),
    mime_type: str = Form(...),
    nonce: str = Form(...),
    key: str = Form(...),
    payload: UploadFile = File(...),
    x_wall_csrf: str | None = Header(None),
):
    session = await wall_sessions.require(request, x_wall_csrf)
    if not await wall_sessions.allow_action(request, "publish", 15):
        raise HTTPException(status_code=429, detail="发送过于频繁，请稍后重试")
    maximum = wall_store.maximum_image_ciphertext_size if kind == "image" else TEXT_CIPHERTEXT_LIMIT
    ciphertext = await _read_encrypted_payload(payload, maximum)
    try:
        message = await wall_store.publish(
            kind=kind,
            mime_type=mime_type,
            nonce=nonce,
            encoded_key=key,
            ciphertext=ciphertext,
            avatar_id=session.avatar_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"message": message}


@router.post("/messages/{message_id}/reveal")
async def reveal_message(
    message_id: str,
    request: Request,
    x_wall_csrf: str | None = Header(None),
):
    await wall_sessions.require(request, x_wall_csrf)
    try:
        envelope = await wall_store.reveal(message_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="消息不存在或已经焚毁") from exc
    if not envelope:
        raise HTTPException(status_code=410, detail="消息不存在或已经焚毁")
    return Response(
        content=envelope.ciphertext,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Wall-Key": envelope.key,
            "X-Wall-Nonce": envelope.nonce,
            "X-Wall-Kind": envelope.kind,
            "X-Wall-Mime": envelope.mime_type,
            "X-Content-Type-Options": "nosniff",
        },
    )
