from __future__ import annotations

import asyncio
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
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.services import karaoke_accounts as accounts
from app.services import karaoke_schema as ks
from app.services import media_crypto, recording_crypto
from app.services.federation import protocol as p
from app.services.federation.runtime import runtime
from app.services.federation.state import state
from app.services.federation import schema as fs

class RecordingRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def bounded(request: Request):
            if request.method == "POST" and self.path.endswith("/recordings/ticket"):
                maximum = recording_crypto.MAX_METADATA_BYTES + 8192
                declared = request.headers.get("content-length", "")
                # Reject oversized declarations before requesting any body, but
                # always count actual chunks: headers cannot establish the bound.
                digits = declared.lstrip("0") or "0"
                if declared.isascii() and declared.isdecimal() and (len(digits) > 10 or int(digits) > maximum):
                    raise HTTPException(413, "录音歌词快照消息超过限制", headers={"Cache-Control":"private, no-store"})
                chunks, length = [], 0
                async for chunk in request.stream():
                    length += len(chunk)
                    if length > maximum:
                        raise HTTPException(413, "录音歌词快照消息超过限制", headers={"Cache-Control":"private, no-store"})
                    chunks.append(chunk)
                request._body=b"".join(chunks)
            return await original(request)
        return bounded


router = APIRouter(prefix="/account", route_class=RecordingRoute)
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


class TicketPayload(BaseModel):
    size_bytes: StrictInt = Field(gt=0, le=MAX_RECORDING_BYTES)
    content_type: str = Field(min_length=1, max_length=96)
    media: str | None = Field(None, min_length=80, max_length=512)
    title: str | None = Field(None, max_length=255)
    lyrics: list[dict] | None = Field(None, max_length=10000)
    encrypted_lyrics: dict | None = None
    preparation_token: str = Field("", max_length=2048)


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
            "storage_available": bool(await accounts.available_storage_nodes())}


@router.post("/logout")
async def logout(request: Request, response: Response):
    user = await accounts.current_user(request, mutation=True)
    raw = request.cookies.get(accounts.cookie_names()[0], "")
    if raw:
        await accounts.redis_client.delete(accounts.SESSION_PREFIX + accounts.hashlib.sha256(raw.encode()).hexdigest())
    accounts.clear_session_cookies(response)
    await accounts.audit(request, user["user_id"], "logout", "success")
    return {"status": "ok"}


@router.post("/password")
async def change_password(request: Request, payload: PasswordPayload):
    user = await _user(request, True)
    if not await accounts.verify_password(payload.current_password, user["password_hash"]):
        await accounts.audit(request, user["user_id"], "password-change", "failure")
        raise HTTPException(400, "当前密码错误")
    try:
        encoded = await accounts.hash_password(payload.new_password)
    except ValueError as exc:
        await accounts.audit(request, user["user_id"], "password-change", "failure",
                             detail={"reason": "password-policy"})
        raise HTTPException(400, str(exc)) from exc
    async with state.database.begin() as conn:
        await conn.execute(update(ks.users).where(ks.users.c.user_id == user["user_id"])
                           .values(password_hash=encoded, updated_at=int(time.time())))
        await accounts.audit(request, user["user_id"], "password-change", "success", conn=conn)
    await accounts.invalidate_user_sessions(user["user_id"])
    return {"status": "ok", "relogin_required": True}


def _safe_filename(title: str, username: str, recording_id: str, content_type: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", title).strip(" ._")[:80] or "卡拉OK录音"
    clean_user = re.sub(r"[^\w\u3400-\u9fff-]+", "_", username)[:32]
    suffix = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
              "audio/mpeg": ".mp3", "audio/wav": ".wav"}.get(content_type, ".bin")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"{stamp}_{clean}_{clean_user}_{recording_id[:8]}{suffix}"


async def _media_metadata(media: str | None, title: str | None, request: Request) -> tuple[str, list[dict]]:
    if not media:
        return str(title or "上传录音")[:255], []
    from app.services import lyrics
    # Ticket owns the shared media fence through reservation commit. Do not
    # acquire a nested lease here, which can deadlock behind a queued rename.
    resolved = await recording_crypto._source_object(media)
    name = Path(resolved["path"]).stem[:255]
    entries = []
    if resolved["kind"] == "audio":
        encrypted = await media_crypto._encrypted_lyric(resolved["path"], resolved["resource_id"])
        if encrypted is None:
            _path, entries = await lyrics.load_for_media(resolved["media_id"])
    return name, entries


async def _recording_and_placement(recording_id: str, user_id: str) -> tuple[dict, dict, dict | None]:
    async with state.database.connect() as conn:
        row = (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.recording_id == recording_id, ks.recordings.c.user_id == user_id,
        ))).mappings().first()
    if not row:
        raise HTTPException(404, "录音不存在")
    member_id = row.get("storage_member_id")
    from app.services import resource_pool
    member = next((item for item in await resource_pool.list_members(state.database)
                   if item["member_id"] == member_id), None)
    if not member or member["health"] != "online":
        raise HTTPException(503, "录音存储节点不可用")
    relation = None
    if member["relationship_id"]:
        try:
            relation = await state.relationship(member["relationship_id"])
        except p.ProtocolError as exc:
            raise HTTPException(503, "录音存储节点不可用") from exc
        if relation["state"] != "active" or relation["status"] == "offline":
            raise HTTPException(503, "录音存储节点不可用")
    return dict(row), member, relation


def _capability(relation: dict, row: dict, operation: str) -> str:
    _lyrics, snapshot, expected = recording_crypto.decode_lyrics(row["lyrics"])
    descriptor = expected["expected_encrypted_lyrics"] if expected else snapshot["encryption"] if snapshot else None
    digest = expected["encrypted_lyrics_sha256"] if expected else recording_crypto.metadata_sha256(
        {"title": row["title"], "lyrics": [], "encrypted_lyrics": snapshot}) if snapshot else ""
    return p.recording_token(state.unseal(relation["credential"]), relation["relationship_id"],
                             state.node["node_id"], row["user_id"], row["recording_id"], operation,
                             int(time.time()), size=int(row["size_bytes"]),
                             content_type=row["content_type"], filename=row["filename"],
                             encrypted_lyrics_encryption=descriptor, encrypted_lyrics_sha256=digest)


async def _member_and_relation(member_id: str) -> tuple[dict, dict | None]:
    from app.services import resource_pool
    member = next((item for item in await resource_pool.list_members(state.database)
                   if item["member_id"] == member_id), None)
    if not member:
        raise HTTPException(503, "录音存储节点不可用")
    relation = await state.relationship(member["relationship_id"]) if member["relationship_id"] else None
    return member, relation


async def _remove_recording_bytes(row: dict) -> None:
    member, relation = await _member_and_relation(row["storage_member_id"])
    if relation is None:
        from app.services import karaoke_storage
        karaoke_storage.remove(member["member_id"], row["user_id"], row["recording_id"])
    else:
        await runtime.call(relation, f"/internal/v1/recordings/{row['recording_id']}/delete",
                           {"user_id": row["user_id"]})


async def _recording_stat(row: dict) -> dict:
    member, relation = await _member_and_relation(row["storage_member_id"])
    if relation is None:
        from app.services import karaoke_storage
        return await asyncio.to_thread(karaoke_storage.stat, member["member_id"], row["user_id"], row["recording_id"])
    return await runtime.call(relation, f"/internal/v1/recordings/{row['recording_id']}/stat",
                              {"user_id": row["user_id"]})


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
            await _remove_recording_bytes(dict(stale_row))
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
            await media_crypto.forget_encryption(conn, current["recording_id"], object_kind="recording_lyric")
            await conn.execute(delete(ks.recordings).where(
                ks.recordings.c.recording_id == current["recording_id"]
            ))
            await conn.execute(update(ks.users).where(ks.users.c.user_id == user_id).values(
                used_bytes=func.greatest(0, ks.users.c.used_bytes - size), updated_at=now,
            ))
            await conn.execute(update(fs.storage_members).where(
                fs.storage_members.c.member_id == current["storage_member_id"]
            ).values(reserved_bytes=func.greatest(0, fs.storage_members.c.reserved_bytes - size)))


@router.post("/recordings/ticket")
async def create_ticket(request: Request, payload: TicketPayload):
    user = await _user(request, True)
    from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        try:
            return await _create_ticket(request, payload, user)
        except media_crypto.SessionError as exc:
            raise HTTPException(401, "录音歌词加密授权已失效，请重新准备快照") from exc
        except media_crypto.MetadataError as exc:
            raise HTTPException(400, str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(409, "录音或加密快照身份已被占用，请重新准备") from exc


async def _create_ticket(request: Request, payload: TicketPayload, user: dict):
    if payload.content_type.split(";", 1)[0].lower() not in ALLOWED_TYPES:
        raise HTTPException(415, "只允许上传受支持的录音音频")
    from app.services import resource_pool
    try:
        member = await resource_pool.choose_member(payload.size_bytes, database=state.database)
    except p.ProtocolError as exc:
        raise HTTPException(507, str(exc)) from exc
    title, lyrics = await _media_metadata(payload.media, payload.title, request)
    title = payload.title.strip() if payload.title and payload.title.strip() else title
    stored_lyrics, descriptor = lyrics, None
    encrypted_source = None
    if payload.media:
        source = await recording_crypto._source_object(payload.media)
        if source["kind"] == "audio":
            encrypted_source = await media_crypto._encrypted_lyric(source["path"], source["resource_id"])
    if encrypted_source is not None:
        from app.api.v1.media_crypto import available, browser_binding, same_origin
        await available(request)
        same_origin(request)
        if payload.encrypted_lyrics is None or payload.lyrics:
            raise media_crypto.MetadataError("加密歌词必须提供浏览器加密的录音快照")
        snapshot = recording_crypto.encrypted_snapshot(payload.encrypted_lyrics)
        descriptor = media_crypto.Metadata.from_dict(snapshot["encryption"])
        browser = browser_binding(request, Response(), create=False)
        binding = recording_crypto.preparation_binding(user["user_id"], browser, payload.media, encrypted_source["encryption"].file_id)
        media_crypto.manager.verify_preparation(binding, payload.preparation_token, descriptor)
        stored_lyrics = recording_crypto.reservation(snapshot, title, payload.size_bytes)
    elif payload.encrypted_lyrics is not None or payload.preparation_token:
        raise media_crypto.MetadataError("加密快照需要当前加密歌词来源")
    elif payload.lyrics is not None:
        stored_lyrics = recording_crypto.lyric_array(payload.lyrics)
    now, recording_id = int(time.time()), uuid.uuid4().hex
    filename = _safe_filename(title, user["username"], recording_id, payload.content_type.split(";", 1)[0].lower())
    await _cleanup_stale_pending(user["user_id"], now)
    async with state.database.begin() as conn:
        locked = (await conn.execute(select(ks.users).where(ks.users.c.user_id == user["user_id"])
                                     .with_for_update())).mappings().one()
        if locked["status"] != "active" or locked["used_bytes"] + payload.size_bytes > locked["quota_bytes"]:
            raise HTTPException(413, "个人录音空间不足")
        storage = (await conn.execute(select(fs.storage_members).where(
            fs.storage_members.c.member_id == member["member_id"],
            fs.storage_members.c.storage_enabled == 1,
            fs.storage_members.c.health == "online",
        ).with_for_update())).mappings().first()
        if not storage or not storage["writable"]:
            raise HTTPException(409, "存储调度目标当前不可用")
        if int(storage["used_bytes"]) + int(storage["reserved_bytes"]) + payload.size_bytes > int(storage["allocated_bytes"]):
            raise HTTPException(413, "录音存储节点空间不足")
        await conn.execute(update(ks.users).where(ks.users.c.user_id == user["user_id"])
                           .values(used_bytes=locked["used_bytes"] + payload.size_bytes, updated_at=now))
        await conn.execute(update(fs.storage_members).where(
            fs.storage_members.c.member_id == member["member_id"]
        ).values(reserved_bytes=storage["reserved_bytes"] + payload.size_bytes))
        await conn.execute(insert(ks.recordings).values(
            recording_id=recording_id, user_id=user["user_id"],
            storage_member_id=member["member_id"],
            filename=filename, content_type=payload.content_type.split(";", 1)[0].lower(),
            size_bytes=payload.size_bytes, sha256=None, state="pending", title=title,
            lyrics=stored_lyrics, created_at=now, updated_at=now,
        ))
        await media_crypto.put_encryption(conn, recording_id, descriptor, object_kind="recording_lyric")
        await accounts.audit(request, user["user_id"], "recording-reserve", "success", detail={
            "recording_id": recording_id, "size_bytes": payload.size_bytes, "storage_member_id": member["member_id"],
        }, conn=conn)
    row, member, relation = await _recording_and_placement(recording_id, user["user_id"])
    direct = bool(relation and relation["mode"] == "Direct")
    upload_url = (relation["peer_endpoint"] + f"/internal/v1/recordings/{recording_id}"
                  if direct else f"/api/v1/karaoke/account/recordings/{recording_id}/content")
    return {"recording_id": recording_id, "filename": filename, "upload_url": upload_url,
            "direct": direct, "capability": _capability(relation, row, "upload") if direct else None}


@router.put("/recordings/{recording_id}/content")
async def relay_upload(recording_id: str, request: Request):
    user = await _user(request, True)
    row, member, relation = await _recording_and_placement(recording_id, user["user_id"])
    if row["state"] != "pending" or (relation is not None and relation["mode"] != "Relay"):
        raise HTTPException(409, "录音上传状态或节点传输方式已变化")
    if relation is None:
        from app.services import karaoke_storage
        _lyrics, snapshot, expected = recording_crypto.decode_lyrics(row["lyrics"])
        value = {"size": int(row["size_bytes"]), "u": row["user_id"], "i": recording_id}
        if expected:
            value.update(encrypted_lyrics_encryption=expected["expected_encrypted_lyrics"],
                         encrypted_lyrics_sha256=expected["encrypted_lyrics_sha256"])
        return JSONResponse(await karaoke_storage.receive(request, {
            "relationship_id": member["member_id"], "allocated_capacity_bytes": int(member["allocated_bytes"]),
        }, value))
    token = _capability(relation, row, "upload")
    headers = {"X-Recording-Capability": token, "Content-Type": row["content_type"],
               "Content-Length": str(row["size_bytes"]), "Accept-Encoding": "identity"}
    timeout = httpx.Timeout(330, connect=8)
    async with httpx.AsyncClient(verify=ssl.create_default_context(), trust_env=False, timeout=timeout) as client:
        from app.services.upload_claims import chunks
        async with client.stream("PUT", relation["peer_endpoint"] + f"/internal/v1/recordings/{recording_id}",
                                 headers=headers, content=chunks(request)) as upstream:
            body_parts, response_size = [], 0
            async for part in upstream.aiter_bytes():
                response_size += len(part)
                if response_size > p.MAX_LYRIC_RESPONSE_BYTES:
                    raise HTTPException(502, "录音存储节点返回内容超过限制")
                body_parts.append(part)
            body = b"".join(body_parts)
            if upstream.status_code != 200:
                raise HTTPException(502, f"录音存储节点上传失败（HTTP {upstream.status_code}）")
    return JSONResponse(json.loads(body))


@router.post("/recordings/{recording_id}/finalize")
async def finalize(recording_id: str, request: Request):
    user = await _user(request, True)
    row, member, relation = await _recording_and_placement(recording_id, user["user_id"])
    if row["state"] == "ready":
        return {"status": "ready", "recording_id": recording_id}
    if row["state"] != "pending":
        raise HTTPException(409, "录音不在可确认状态")
    result = await _recording_stat(row)
    actual = int(result.get("size_bytes") or 0)
    sha256 = str(result.get("sha256") or "")
    if actual <= 0 or actual > int(row["size_bytes"]) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise HTTPException(502, "录音存储节点返回无效文件状态")
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    if not recording_crypto.receipt_matches(row, metadata):
        raise HTTPException(409, "录音文件的歌词快照与预留不一致")
    metadata = recording_crypto.metadata(metadata) if metadata else {}
    if metadata.get("encrypted_lyrics"):
        try:
            recording_crypto.reservation(metadata["encrypted_lyrics"], metadata["title"], actual)
        except media_crypto.MetadataError as exc:
            raise HTTPException(502, "录音实际大小无法容纳完整的加密歌词快照") from exc
    title = str(metadata.get("title") or row["title"])[:255]
    lyric_entries = ({"encrypted_lyrics": metadata["encrypted_lyrics"]} if metadata.get("encrypted_lyrics")
                     else metadata.get("lyrics") if isinstance(metadata.get("lyrics"), list) else row["lyrics"])
    async with state.database.begin() as conn:
        locked = (await conn.execute(select(ks.recordings).where(ks.recordings.c.recording_id == recording_id)
                                     .with_for_update())).mappings().one()
        if locked["state"] == "pending":
            if not recording_crypto.receipt_matches(dict(locked), metadata):
                raise HTTPException(409, "录音预留已变化")
            _lyrics, snapshot, _expected = recording_crypto.decode_lyrics(lyric_entries)
            stored_descriptor = await media_crypto.encryption_for_id(conn, recording_id, object_kind="recording_lyric")
            if stored_descriptor != (media_crypto.Metadata.from_dict(snapshot["encryption"]) if snapshot else None):
                raise HTTPException(409, "录音歌词加密身份不一致")
            difference = actual - int(locked["size_bytes"])
            await conn.execute(update(ks.users).where(ks.users.c.user_id == user["user_id"])
                               .values(used_bytes=ks.users.c.used_bytes + difference, updated_at=int(time.time())))
            await conn.execute(update(fs.storage_members).where(
                fs.storage_members.c.member_id == locked["storage_member_id"]
            ).values(reserved_bytes=func.greatest(
                0, fs.storage_members.c.reserved_bytes - int(locked["size_bytes"])
            ), used_bytes=fs.storage_members.c.used_bytes + actual))
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
    row, member, relation = await _recording_and_placement(recording_id, user["user_id"])
    if row["state"] != "pending":
        raise HTTPException(409, "录音已完成，不能取消预留")
    await _remove_recording_bytes(row)
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
        await conn.execute(update(fs.storage_members).where(
            fs.storage_members.c.member_id == locked["storage_member_id"]
        ).values(reserved_bytes=func.greatest(
            0, fs.storage_members.c.reserved_bytes - int(locked["size_bytes"])
        )))
        await accounts.audit(request, user["user_id"], "recording-cancel", "success",
                             detail={"recording_id": recording_id}, conn=conn)
    return {"status": "cancelled"}


def _recording_json(row) -> dict:
    result = {key: row[key] for key in ("recording_id", "filename", "content_type", "size_bytes",
                                      "sha256", "title", "created_at")}
    lyrics, snapshot, expected = recording_crypto.decode_lyrics(row["lyrics"])
    if expected:
        raise media_crypto.MetadataError("Pending recording cannot be presented as ready")
    result["lyrics"] = lyrics
    if snapshot:
        result["encrypted_lyrics"] = snapshot
    return result


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
    row, member, relation = await _recording_and_placement(recording_id, user["user_id"])
    if row["state"] != "ready":
        raise HTTPException(404, "录音不存在")
    disposition = ("attachment" if download else "inline") + f"; filename*=UTF-8''{quote(row['filename'])}"
    if relation is None:
        from app.services import karaoke_storage
        response = Response(headers={"X-Accel-Redirect": karaoke_storage.protected_redirect(
            member["member_id"], row["user_id"], recording_id)})
    elif relation["mode"] == "Direct":
        token = _capability(relation, row, "download" if download else "stream")
        response = RedirectResponse(relation["peer_endpoint"] + f"/internal/v1/recordings/{recording_id}", 307)
        # Direct browser requests cannot receive a secret header through redirects, so use a bounded query capability.
        response.headers["Location"] += "?token=" + quote(token)
        response.headers["Referrer-Policy"] = "no-referrer"
    else:
        token = _capability(relation, row, "download" if download else "stream")
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
    row, member, relation = await _recording_and_placement(recording_id, user["user_id"])
    await _remove_recording_bytes(row)
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
        await conn.execute(update(fs.storage_members).where(
            fs.storage_members.c.member_id == locked["storage_member_id"]
        ).values(used_bytes=func.greatest(
            0, fs.storage_members.c.used_bytes - int(locked["size_bytes"])
        )))
        await accounts.audit(request, user["user_id"], "recording-delete", "success", detail={
            "recording_id": recording_id,
        }, conn=conn)
    return {"status": "deleted"}


@router.delete("")
async def delete_account(request: Request, response: Response):
    user = await _user(request, True)
    async with state.database.connect() as conn:
        existing = [dict(row) for row in (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.user_id == user["user_id"]))).mappings()]
    for recording in existing:
        await _remove_recording_bytes(recording)
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
        by_member: dict[str, int] = {}
        for recording in existing:
            by_member[recording["storage_member_id"]] = by_member.get(recording["storage_member_id"], 0) + int(recording["size_bytes"])
        for member_id, removed in by_member.items():
            await conn.execute(update(fs.storage_members).where(
                fs.storage_members.c.member_id == member_id
            ).values(used_bytes=func.greatest(0, fs.storage_members.c.used_bytes - removed),
                     reserved_bytes=func.greatest(0, fs.storage_members.c.reserved_bytes - removed)))
    await accounts.invalidate_user_sessions(user["user_id"])
    accounts.clear_session_cookies(response)
    return {"status": "deleted"}
