from __future__ import annotations

import json
import re
import ssl
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, insert, select, update

from app.services import karaoke_accounts as accounts
from app.services import karaoke_schema as ks
from app.services.federation import protocol as p
from app.services.federation.runtime import runtime
from app.services.federation.state import state
from app.services.federation import schema as fs

router = APIRouter(prefix="/account")
MAX_RECORDING_BYTES = 1024 ** 3
ALLOWED_TYPES = {"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "application/octet-stream"}


class AuthPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    challenge: str | None = Field(None, max_length=32)
    captcha: str | None = Field(None, max_length=12)
    webrtc_addresses: list[str] = Field(default_factory=list, max_length=8)


class PasswordPayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class BindPayload(BaseModel):
    relationship_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class TicketPayload(BaseModel):
    size_bytes: int = Field(gt=0, le=MAX_RECORDING_BYTES)
    content_type: str = Field(min_length=1, max_length=96)
    media: str | None = Field(None, min_length=80, max_length=512)
    title: str | None = Field(None, max_length=255)
    storage_relationship_id: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")


async def _user(request: Request, mutation=False):
    accounts.require_master()
    return await accounts.current_user(request, mutation=mutation)


@router.get("/captcha")
async def captcha():
    challenge, answer = await accounts.captcha_create()
    return {"challenge": challenge, "image_url": f"/api/v1/karaoke/account/captcha/{challenge}"}


@router.get("/captcha/{challenge}", include_in_schema=False)
async def captcha_image(challenge: str):
    stored = await accounts.redis_client.get(accounts.CAPTCHA_PREFIX + challenge)
    if not stored:
        raise HTTPException(404, "验证码已过期")
    # The answer cannot be recovered from its hash; issue SVG at challenge creation through a short sibling key.
    answer = await accounts.redis_client.get(accounts.CAPTCHA_PREFIX + challenge + ":image")
    if not answer:
        raise HTTPException(404, "验证码已过期")
    return Response(accounts.captcha_svg(answer), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/register")
async def register(request: Request, payload: AuthPayload, response: Response):
    user = await accounts.register(request, response, payload.username, payload.password,
                                   payload.challenge, payload.captcha, payload.webrtc_addresses)
    return {"status": "ok", "user": user}


@router.post("/login")
async def login(request: Request, payload: AuthPayload, response: Response):
    user = await accounts.login(request, response, payload.username, payload.password,
                                payload.challenge, payload.captcha, payload.webrtc_addresses)
    return {"status": "ok", "user": user}


@router.get("/status")
async def status(request: Request):
    if state.node.get("role") != "Master":
        return {"available": False, "authenticated": False, "reason": "请在 Master 节点使用账号与录音"}
    user = await accounts.current_user(request, optional=True)
    return {"available": True, "authenticated": bool(user),
            "user": accounts.public_user(user) if user else None,
            "storage_nodes": await accounts.available_storage_nodes()}


@router.post("/logout")
async def logout(request: Request, response: Response):
    user = await accounts.current_user(request, mutation=True)
    raw = request.cookies.get(accounts.cookie_names()[0], "")
    if raw:
        await accounts.redis_client.delete(accounts.SESSION_PREFIX + accounts.hashlib.sha256(raw.encode()).hexdigest())
    accounts.clear_session_cookies(response)
    await accounts.audit(request, user["user_id"], "logout", "success")
    return {"status": "ok"}


@router.post("/bind")
async def bind_storage(request: Request, payload: BindPayload):
    user = await _user(request, True)
    nodes = {item["relationship_id"]: item for item in await accounts.available_storage_nodes()}
    if payload.relationship_id not in nodes:
        raise HTTPException(409, "所选存储节点当前不可用")
    async with state.database.begin() as conn:
        locked = (await conn.execute(select(ks.users).where(ks.users.c.user_id == user["user_id"])
                                     .with_for_update())).mappings().one()
        if (locked["storage_relationship_id"] and locked["storage_relationship_id"] != payload.relationship_id
                and int(locked["used_bytes"]) > 0):
            raise HTTPException(409, "账号已有录音，不能更换存储节点")
        await conn.execute(update(ks.users).where(ks.users.c.user_id == user["user_id"])
                           .values(storage_relationship_id=payload.relationship_id, updated_at=int(time.time())))
        await accounts.audit(request, user["user_id"], "storage-bind", "success", detail={
            "relationship_id": payload.relationship_id,
        }, conn=conn)
    return {"status": "ok", "storage_relationship_id": payload.relationship_id}


@router.post("/password")
async def change_password(request: Request, payload: PasswordPayload):
    user = await _user(request, True)
    if not await accounts.verify_password(payload.current_password, user["password_hash"]):
        await accounts.audit(request, user["user_id"], "password-change", "failure")
        raise HTTPException(400, "当前密码错误")
    encoded = await accounts.hash_password(payload.new_password)
    async with state.database.begin() as conn:
        await conn.execute(update(ks.users).where(ks.users.c.user_id == user["user_id"])
                           .values(password_hash=encoded, updated_at=int(time.time())))
        await accounts.audit(request, user["user_id"], "password-change", "success", conn=conn)
    await accounts.invalidate_user_sessions(user["user_id"])
    return {"status": "ok", "relogin_required": True}


def _safe_filename(title: str, username: str, recording_id: str, content_type: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", title).strip(" ._")[:80] or "K歌录音"
    clean_user = re.sub(r"[^\w\u3400-\u9fff-]+", "_", username)[:32]
    suffix = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
              "audio/mpeg": ".mp3", "audio/wav": ".wav"}.get(content_type, ".bin")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"{stamp}_{clean}_{clean_user}_{recording_id[:8]}{suffix}"


async def _media_metadata(media: str | None, title: str | None, request: Request) -> tuple[str, list[dict]]:
    if not media:
        return str(title or "上传录音")[:255], []
    from app.api.v1.karaoke import _resolve
    from app.services import lyrics
    from app.services.federation import routing
    resolved = await _resolve(media, request)
    name = Path(resolved["path"]).stem[:255]
    entries = []
    if resolved["has_lyrics"]:
        if resolved["kind"] == "remote":
            entries = await routing.lyric_entries(resolved["identifier"])
        else:
            _path, entries = await lyrics.load_for_track(resolved["path"])
    return name, entries


async def _recording_and_relation(recording_id: str, user_id: str) -> tuple[dict, dict]:
    async with state.database.connect() as conn:
        row = (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.recording_id == recording_id, ks.recordings.c.user_id == user_id,
        ))).mappings().first()
    if not row:
        raise HTTPException(404, "录音不存在")
    try:
        relation = await state.relationship(row["storage_relationship_id"])
    except p.ProtocolError as exc:
        raise HTTPException(503, "录音存储节点不可用") from exc
    if relation["state"] != "active" or relation["status"] == "offline":
        raise HTTPException(503, "录音存储节点不可用")
    return dict(row), relation


def _capability(relation: dict, row: dict, operation: str) -> str:
    return p.recording_token(state.unseal(relation["credential"]), relation["relationship_id"],
                             state.node["node_id"], row["user_id"], row["recording_id"], operation,
                             int(time.time()), size=int(row["size_bytes"]),
                             content_type=row["content_type"], filename=row["filename"])


async def _cleanup_stale_pending(user_id: str, now: int) -> None:
    async with state.database.begin() as conn:
        stale = (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.user_id == user_id,
            ks.recordings.c.state == "pending",
            ks.recordings.c.created_at < now - 3600,
        ).limit(20).with_for_update())).mappings().all()
        if stale:
            await conn.execute(update(ks.recordings).where(
                ks.recordings.c.recording_id.in_([item["recording_id"] for item in stale])
            ).values(state="deleting", updated_at=now))
    for stale_row in stale:
        removed = False
        try:
            relation = await state.relationship(stale_row["storage_relationship_id"])
            await runtime.call(relation, f"/internal/v1/recordings/{stale_row['recording_id']}/delete", {
                "user_id": user_id,
            })
            removed = True
        except Exception:
            pass
        async with state.database.begin() as conn:
            current = (await conn.execute(select(ks.recordings).where(
                ks.recordings.c.recording_id == stale_row["recording_id"],
                ks.recordings.c.user_id == user_id,
            ).with_for_update())).mappings().first()
            if not current or current["state"] != "deleting":
                continue
            if not removed:
                await conn.execute(update(ks.recordings).where(
                    ks.recordings.c.recording_id == current["recording_id"]
                ).values(state="pending", updated_at=now))
                continue
            size = int(current["size_bytes"])
            await conn.execute(delete(ks.recordings).where(
                ks.recordings.c.recording_id == current["recording_id"]
            ))
            await conn.execute(update(ks.users).where(ks.users.c.user_id == user_id).values(
                used_bytes=func.greatest(0, ks.users.c.used_bytes - size), updated_at=now,
            ))
            await conn.execute(update(fs.relationships).where(
                fs.relationships.c.relationship_id == current["storage_relationship_id"]
            ).values(recording_used_bytes=func.greatest(
                0, fs.relationships.c.recording_used_bytes - size
            )))


@router.post("/recordings/ticket")
async def create_ticket(request: Request, payload: TicketPayload):
    user = await _user(request, True)
    if payload.content_type.split(";", 1)[0].lower() not in ALLOWED_TYPES:
        raise HTTPException(415, "只允许上传受支持的录音音频")
    nodes = {item["relationship_id"]: item for item in await accounts.available_storage_nodes()}
    relation_id = user.get("storage_relationship_id")
    if not relation_id and payload.storage_relationship_id:
        relation_id = payload.storage_relationship_id
    if not relation_id:
        raise HTTPException(409, "首次上传前请选择录音存储节点", headers={"X-Storage-Binding-Required": "1"})
    if relation_id not in nodes:
        raise HTTPException(409, "绑定的录音存储节点当前不可用")
    title, lyrics = await _media_metadata(payload.media, payload.title, request)
    now, recording_id = int(time.time()), uuid.uuid4().hex
    filename = _safe_filename(title, user["username"], recording_id, payload.content_type.split(";", 1)[0].lower())
    await _cleanup_stale_pending(user["user_id"], now)
    async with state.database.begin() as conn:
        locked = (await conn.execute(select(ks.users).where(ks.users.c.user_id == user["user_id"])
                                     .with_for_update())).mappings().one()
        if locked["status"] != "active" or locked["used_bytes"] + payload.size_bytes > locked["quota_bytes"]:
            raise HTTPException(413, "个人录音空间不足")
        if not locked["storage_relationship_id"]:
            await conn.execute(update(ks.users).where(ks.users.c.user_id == user["user_id"])
                               .values(storage_relationship_id=relation_id, updated_at=now))
        elif locked["storage_relationship_id"] != relation_id:
            raise HTTPException(409, "录音只能上传到账号已绑定的存储节点")
        storage = (await conn.execute(select(fs.relationships).where(
            fs.relationships.c.relationship_id == relation_id,
            fs.relationships.c.direction == "downstream",
            fs.relationships.c.state == "active",
        ).with_for_update())).mappings().first()
        if not storage or not storage["recording_storage_enabled"]:
            raise HTTPException(409, "绑定的录音存储节点当前不可用")
        if int(storage["recording_used_bytes"]) + payload.size_bytes > int(storage["recording_capacity_bytes"]):
            raise HTTPException(413, "录音存储节点空间不足")
        await conn.execute(update(ks.users).where(ks.users.c.user_id == user["user_id"])
                           .values(used_bytes=locked["used_bytes"] + payload.size_bytes, updated_at=now))
        await conn.execute(update(fs.relationships).where(
            fs.relationships.c.relationship_id == relation_id
        ).values(recording_used_bytes=storage["recording_used_bytes"] + payload.size_bytes))
        await conn.execute(insert(ks.recordings).values(
            recording_id=recording_id, user_id=user["user_id"], storage_relationship_id=relation_id,
            filename=filename, content_type=payload.content_type.split(";", 1)[0].lower(),
            size_bytes=payload.size_bytes, sha256=None, state="pending", title=title,
            lyrics=lyrics, created_at=now, updated_at=now,
        ))
        await accounts.audit(request, user["user_id"], "recording-reserve", "success", detail={
            "recording_id": recording_id, "size_bytes": payload.size_bytes, "relationship_id": relation_id,
        }, conn=conn)
    row, relation = await _recording_and_relation(recording_id, user["user_id"])
    direct = relation["mode"] == "Direct"
    upload_url = (relation["peer_endpoint"] + f"/internal/v1/recordings/{recording_id}"
                  if direct else f"/api/v1/karaoke/account/recordings/{recording_id}/content")
    return {"recording_id": recording_id, "filename": filename, "upload_url": upload_url,
            "direct": direct, "capability": _capability(relation, row, "upload") if direct else None}


@router.put("/recordings/{recording_id}/content")
async def relay_upload(recording_id: str, request: Request):
    user = await _user(request, True)
    row, relation = await _recording_and_relation(recording_id, user["user_id"])
    if row["state"] != "pending" or relation["mode"] != "Relay":
        raise HTTPException(409, "录音上传状态或节点传输方式已变化")
    token = _capability(relation, row, "upload")
    headers = {"X-Recording-Capability": token, "Content-Type": row["content_type"],
               "Content-Length": str(row["size_bytes"]), "Accept-Encoding": "identity"}
    timeout = httpx.Timeout(330, connect=8)
    async with httpx.AsyncClient(verify=ssl.create_default_context(), trust_env=False, timeout=timeout) as client:
        async with client.stream("PUT", relation["peer_endpoint"] + f"/internal/v1/recordings/{recording_id}",
                                 headers=headers, content=request.stream()) as upstream:
            body = await upstream.aread()
            if upstream.status_code != 200:
                raise HTTPException(502, f"录音存储节点上传失败（HTTP {upstream.status_code}）")
    return JSONResponse(json.loads(body))


@router.post("/recordings/{recording_id}/finalize")
async def finalize(recording_id: str, request: Request):
    user = await _user(request, True)
    row, relation = await _recording_and_relation(recording_id, user["user_id"])
    if row["state"] == "ready":
        return {"status": "ready", "recording_id": recording_id}
    if row["state"] != "pending":
        raise HTTPException(409, "录音不在可确认状态")
    result = await runtime.call(relation, f"/internal/v1/recordings/{recording_id}/stat",
                                {"user_id": user["user_id"]})
    actual = int(result.get("size_bytes") or 0)
    sha256 = str(result.get("sha256") or "")
    if actual <= 0 or actual > int(row["size_bytes"]) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise HTTPException(502, "录音存储节点返回无效文件状态")
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    title = str(metadata.get("title") or row["title"])[:255]
    lyric_entries = metadata.get("lyrics") if isinstance(metadata.get("lyrics"), list) else row["lyrics"]
    async with state.database.begin() as conn:
        locked = (await conn.execute(select(ks.recordings).where(ks.recordings.c.recording_id == recording_id)
                                     .with_for_update())).mappings().one()
        if locked["state"] == "pending":
            difference = actual - int(locked["size_bytes"])
            await conn.execute(update(ks.users).where(ks.users.c.user_id == user["user_id"])
                               .values(used_bytes=ks.users.c.used_bytes + difference, updated_at=int(time.time())))
            await conn.execute(update(fs.relationships).where(
                fs.relationships.c.relationship_id == locked["storage_relationship_id"]
            ).values(recording_used_bytes=func.greatest(
                0, fs.relationships.c.recording_used_bytes + difference
            )))
            await conn.execute(update(ks.recordings).where(ks.recordings.c.recording_id == recording_id).values(
                size_bytes=actual, sha256=sha256, state="ready", title=title, lyrics=lyric_entries,
                updated_at=int(time.time()),
            ))
            await accounts.audit(request, user["user_id"], "recording-finalize", "success", detail={
                "recording_id": recording_id, "size_bytes": actual, "sha256": sha256,
            }, conn=conn)
    return {"status": "ready", "recording_id": recording_id}


@router.delete("/recordings/{recording_id}/pending")
async def cancel_pending(recording_id: str, request: Request):
    user = await _user(request, True)
    row, relation = await _recording_and_relation(recording_id, user["user_id"])
    if row["state"] != "pending":
        raise HTTPException(409, "录音已完成，不能取消预留")
    await runtime.call(relation, f"/internal/v1/recordings/{recording_id}/delete", {"user_id": user["user_id"]})
    async with state.database.begin() as conn:
        locked = (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.recording_id == recording_id,
            ks.recordings.c.user_id == user["user_id"],
        ).with_for_update())).mappings().first()
        if not locked:
            return {"status": "cancelled"}
        if locked["state"] != "pending":
            raise HTTPException(409, "录音已完成，不能取消预留")
        await conn.execute(delete(ks.recordings).where(ks.recordings.c.recording_id == recording_id))
        await conn.execute(update(ks.users).where(ks.users.c.user_id == user["user_id"])
                           .values(used_bytes=func.greatest(
                               0, ks.users.c.used_bytes - int(locked["size_bytes"])
                           ), updated_at=int(time.time())))
        await conn.execute(update(fs.relationships).where(
            fs.relationships.c.relationship_id == locked["storage_relationship_id"]
        ).values(recording_used_bytes=func.greatest(
            0, fs.relationships.c.recording_used_bytes - int(locked["size_bytes"])
        )))
        await accounts.audit(request, user["user_id"], "recording-cancel", "success",
                             detail={"recording_id": recording_id}, conn=conn)
    return {"status": "cancelled"}


def _recording_json(row) -> dict:
    return {key: row[key] for key in ("recording_id", "filename", "content_type", "size_bytes",
                                      "sha256", "title", "lyrics", "created_at")}


@router.get("/recordings")
async def recordings(request: Request):
    user = await _user(request)
    async with state.database.connect() as conn:
        rows = (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.user_id == user["user_id"], ks.recordings.c.state == "ready",
        ).order_by(ks.recordings.c.created_at.desc()).limit(500))).mappings().all()
    return {"items": [_recording_json(row) for row in rows]}


async def _download_response(recording_id: str, request: Request, download: bool):
    user = await _user(request)
    row, relation = await _recording_and_relation(recording_id, user["user_id"])
    if row["state"] != "ready":
        raise HTTPException(404, "录音不存在")
    token = _capability(relation, row, "download" if download else "stream")
    disposition = ("attachment" if download else "inline") + f"; filename*=UTF-8''{quote(row['filename'])}"
    if relation["mode"] == "Direct":
        response = RedirectResponse(relation["peer_endpoint"] + f"/internal/v1/recordings/{recording_id}", 307)
        # Direct browser requests cannot receive a secret header through redirects, so use a bounded query capability.
        response.headers["Location"] += "?token=" + quote(token)
        response.headers["Referrer-Policy"] = "no-referrer"
    else:
        parsed = urlsplit(p.endpoint(relation["peer_endpoint"]))
        internal = f"/_relay_recording/{parsed.hostname}/{parsed.port or 443}/{recording_id}/{token}"
        response = Response(headers={"X-Accel-Redirect": internal})
    response.headers.update({"Content-Disposition": disposition, "Content-Type": row["content_type"],
                             "Cache-Control": "private, no-store"})
    return response


@router.get("/recordings/{recording_id}/stream", include_in_schema=False)
async def recording_stream(recording_id: str, request: Request):
    return await _download_response(recording_id, request, False)


@router.get("/recordings/{recording_id}/download", include_in_schema=False)
async def recording_download(recording_id: str, request: Request):
    return await _download_response(recording_id, request, True)


@router.delete("/recordings/{recording_id}")
async def delete_recording(recording_id: str, request: Request):
    user = await _user(request, True)
    row, relation = await _recording_and_relation(recording_id, user["user_id"])
    await runtime.call(relation, f"/internal/v1/recordings/{recording_id}/delete", {"user_id": user["user_id"]})
    async with state.database.begin() as conn:
        locked = (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.recording_id == recording_id,
            ks.recordings.c.user_id == user["user_id"],
        ).with_for_update())).mappings().first()
        if not locked:
            return {"status": "deleted"}
        await conn.execute(delete(ks.recordings).where(ks.recordings.c.recording_id == recording_id))
        await conn.execute(update(ks.users).where(ks.users.c.user_id == user["user_id"])
                           .values(used_bytes=func.greatest(
                               0, ks.users.c.used_bytes - int(locked["size_bytes"])
                           ), updated_at=int(time.time())))
        await conn.execute(update(fs.relationships).where(
            fs.relationships.c.relationship_id == locked["storage_relationship_id"]
        ).values(recording_used_bytes=func.greatest(
            0, fs.relationships.c.recording_used_bytes - int(locked["size_bytes"])
        )))
        await accounts.audit(request, user["user_id"], "recording-delete", "success", detail={
            "recording_id": recording_id,
        }, conn=conn)
    return {"status": "deleted"}


@router.delete("")
async def delete_account(request: Request, response: Response):
    user = await _user(request, True)
    relation_id = user.get("storage_relationship_id")
    if relation_id:
        relation = await state.relationship(relation_id)
        await runtime.call(relation, f"/internal/v1/recordings/users/{user['user_id']}/delete", {})
    async with state.database.begin() as conn:
        locked = (await conn.execute(select(ks.users).where(
            ks.users.c.user_id == user["user_id"]
        ).with_for_update())).mappings().first()
        if not locked:
            accounts.clear_session_cookies(response)
            return {"status": "deleted"}
        await accounts.audit(request, user["user_id"], "account-delete", "success", conn=conn)
        await conn.execute(delete(ks.recordings).where(ks.recordings.c.user_id == user["user_id"]))
        await conn.execute(delete(ks.users).where(ks.users.c.user_id == user["user_id"]))
        if relation_id:
            await conn.execute(update(fs.relationships).where(
                fs.relationships.c.relationship_id == relation_id
            ).values(recording_used_bytes=func.greatest(
                0, fs.relationships.c.recording_used_bytes - int(locked["used_bytes"])
            )))
    await accounts.invalidate_user_sessions(user["user_id"])
    accounts.clear_session_cookies(response)
    return {"status": "deleted"}
