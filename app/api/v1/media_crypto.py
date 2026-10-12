"""Authenticated browser-encryption grants and opaque byte delivery."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.api.v1.admin_transport import require_secure_admin_transport, secure_admin_transport
from app.core.client_ip import resolve_client_identity
from app.core.config import settings
from app.services import admin_service, media_crypto as crypto

NO_STORE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"}
COOKIE_NAME = "frontiercloud_browser_crypto"
WORKER_CSP = "default-src 'none'; script-src 'self'; connect-src 'self' https:; object-src 'none'; base-uri 'none'; frame-src 'none'; frame-ancestors 'none'; form-action 'none'"


class CryptoRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def limited(request: Request):
            try:
                if request.method in {"POST", "PUT", "PATCH"}:
                    chunks, length = [], 0
                    async for chunk in request.stream():
                        length += len(chunk)
                        if length > 8192:
                            raise HTTPException(413, "Crypto message too large")
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                response = await original(request)
                response.headers.update(NO_STORE_HEADERS)
                return response
            except crypto.SessionLimitError as exc:
                raise HTTPException(429, "临时密钥会话数量达到限额，请稍后重试", headers={**NO_STORE_HEADERS, "Retry-After": "60"}) from exc
            except crypto.SessionError as exc:
                raise HTTPException(401, "临时密钥授权已失效，请重新建立浏览器会话", headers=NO_STORE_HEADERS) from exc
            except crypto.MetadataError as exc:
                raise HTTPException(400, str(exc), headers=NO_STORE_HEADERS) from exc
            except crypto.PremasterError as exc:
                raise HTTPException(503, "媒体密钥状态异常，请恢复正确的数据库与密钥备份", headers=NO_STORE_HEADERS) from exc
            except HTTPException as exc:
                exc.headers = {**NO_STORE_HEADERS, **(exc.headers or {})}
                raise
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
                response.headers.update(NO_STORE_HEADERS)
                return response
        return limited


router = APIRouter(route_class=CryptoRoute, tags=["MediaCrypto"])


class SessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_key: str = Field(min_length=1, max_length=200)


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plaintext_size: StrictInt = Field(gt=0, le=crypto.MAX_PLAINTEXT_SIZE)
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class KeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    file_path: str = Field("", max_length=4096)
    resource_id: str = Field("", max_length=128)


class RecordingPrepareBody(PrepareBody):
    media: str = Field(min_length=80, max_length=512)


class RecordingKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")


async def available(request: Request) -> crypto.Manager:
    if crypto.manager is None or await crypto.business_role() == "Follower":
        raise HTTPException(409, "存储节点不提供媒体密钥，请访问主节点")
    if not secure_admin_transport(request):
        raise HTTPException(426, "临时密钥只允许通过 HTTPS 或本机回环访问")
    return crypto.manager


def same_origin(request: Request) -> None:
    site = request.headers.get("sec-fetch-site", "")
    if site and site not in {"same-origin", "none"}:
        raise HTTPException(403, "密钥授权只允许同源访问")
    origin = request.headers.get("origin", "")
    if origin:
        try:
            parsed = urlsplit(origin)
            expected = urlsplit(str(request.url))
            if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                    or parsed.netloc != request.headers.get("host", "") or parsed.scheme != expected.scheme
                    and not (parsed.scheme == "https" and resolve_client_identity(request.scope).from_trusted_proxy
                             and request.headers.get("x-forwarded-proto", "").lower() == "https")):
                raise ValueError("Origin mismatch")
        except ValueError as exc:
            raise HTTPException(403, "密钥授权只允许同源访问") from exc


def browser_binding(request: Request, response: Response, *, create: bool) -> str:
    value = request.cookies.get(COOKIE_NAME, "")
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        if not create:
            raise crypto.SessionError("Browser binding required")
        value = secrets.token_hex(32)
        response.set_cookie(COOKIE_NAME, value, path="/", httponly=True, secure=settings.TLS_ENABLED, samesite="strict")
    return "browser:" + value


async def admin_binding(request: Request) -> str:
    await require_secure_admin_transport(request)
    return "admin:" + await admin_service.require_admin(request)


@router.get("/media-crypto-sw.js", include_in_schema=False)
async def worker():
    from app.core.static_assets import STATIC_ROOT
    candidate = STATIC_ROOT / "js" / "compiled" / "media-crypto-sw.js"
    return FileResponse(candidate, media_type="application/javascript",
                        headers={**NO_STORE_HEADERS, "Service-Worker-Allowed": "/", "Content-Security-Policy": WORKER_CSP})


@router.post("/api/v1/media/crypto/session")
async def public_session(request: Request, response: Response, body: SessionBody):
    manager = await available(request)
    same_origin(request)
    binding = browser_binding(request, response, create=True)
    ip = resolve_client_identity(request.scope).ip
    if not crypto.creation_admission.allow(ip, binding):
        raise HTTPException(429, "临时密钥会话创建过于频繁，请稍后重试", headers={"Retry-After": "60"})
    return manager.new_session(binding, body.public_key)


@router.post("/api/v1/media/admin/crypto/session")
async def admin_session(request: Request, body: SessionBody, binding: str = Depends(admin_binding)):
    manager = await available(request)
    same_origin(request)
    return manager.new_session(binding, body.public_key)


@router.post("/api/v1/media/admin/crypto/prepare")
async def admin_prepare(request: Request, body: PrepareBody, binding: str = Depends(admin_binding)):
    manager = await available(request)
    same_origin(request)
    if body.plaintext_size > settings.ADMIN_MAX_UPLOAD_FILE_SIZE:
        raise HTTPException(413, "文件超过单文件上传限制")
    descriptor = crypto.Metadata.new(body.plaintext_size)
    return {"encryption": descriptor.to_dict(), "key_envelope": manager.wrap(binding, body.session_id, descriptor),
            "preparation_token": manager.sign_preparation(binding, descriptor)}


async def key_for(body: KeyBody, binding: str, *, admin: bool):
    if not body.file_path and not body.resource_id:
        raise HTTPException(422, "file_path or resource_id is required")
    obj = await crypto.crypto_object(body.file_path, body.resource_id, admin=admin)
    if obj["encryption"] is None:
        raise HTTPException(409, "该文件为明文落盘")
    source = "/api/v1/media/crypto/bytes" if obj["kind"] == "lyric" else "/api/v1/media/stream"
    query = {"file_path": obj["path"]}
    if body.resource_id:
        query["resource_id"] = body.resource_id
    if admin:
        source = "/api/v1/media/admin/crypto/bytes"
        query = {"file_path": obj["path"]}
    return {"encryption": obj["encryption"].to_dict(),
            "key_envelope": crypto.manager.wrap(binding, body.session_id, obj["encryption"]),
            "source_url": source + "?" + urlencode(query),
            "content_type": mimetypes.guess_type(obj["path"])[0] or "application/octet-stream",
            "filename": Path(obj["path"]).name}


@router.post("/api/v1/media/crypto/key")
async def public_key(request: Request, response: Response, body: KeyBody):
    await available(request)
    same_origin(request)
    return await key_for(body, browser_binding(request, response, create=False), admin=False)


@router.post("/api/v1/media/admin/crypto/key")
async def admin_key(request: Request, body: KeyBody, binding: str = Depends(admin_binding)):
    await available(request)
    same_origin(request)
    return await key_for(body, binding, admin=True)


@router.post("/api/v1/media/crypto/revoke")
async def revoke(request: Request, response: Response):
    same_origin(request)
    try:
        binding = browser_binding(request, response, create=False)
        if crypto.manager:
            crypto.manager.revoke_binding(binding)
    except crypto.SessionError:
        pass
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=settings.TLS_ENABLED, samesite="strict")
    return {"status": "revoked"}


def _range(header: str, size: int) -> tuple[int, int, int]:
    if not header:
        return 0, max(-1, size - 1), 200
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", header.strip()) if len(header) <= 256 else None
    if not match or not any(match.groups()) or size == 0:
        raise HTTPException(416, "Requested range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise HTTPException(416, "Requested range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
        start, end = max(0, size - suffix), size - 1
    else:
        start, end = int(first), min(size - 1, int(last)) if last else size - 1
    if start >= size or start > end:
        raise HTTPException(416, "Requested range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
    return start, end, 206


async def pinned_bytes(request: Request, obj: dict, stream) -> Response:
    info = os.fstat(stream.fileno())
    if info.st_size != obj["size_bytes"]:
        stream.close()
        raise HTTPException(409, "Media source changed")
    try:
        start, end, status = _range(request.headers.get("range", ""), info.st_size)
        count = max(0, end - start + 1)
        headers = {**NO_STORE_HEADERS, "Accept-Ranges": "bytes", "Content-Length": str(count),
                   "X-Media-Object-ID": obj["media_id"]}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{info.st_size}"
        if request.method == "HEAD":
            stream.close()
            return Response(status_code=status, media_type="application/octet-stream", headers=headers)
        stream.seek(start)
    except BaseException:
        stream.close()
        raise
    async def chunks():
        remaining = count
        try:
            while remaining:
                data = await asyncio.to_thread(stream.read, min(1024 * 1024, remaining))
                if not data:
                    raise OSError("Pinned media stream ended before its declared length")
                remaining -= len(data)
                yield data
        finally:
            stream.close()
    return StreamingResponse(chunks(), status_code=status, media_type="application/octet-stream", headers=headers)


async def raw_bytes(request: Request, path: str, *, admin: bool, expected_id: str = "", snapshot: str = ""):
    from sqlalchemy import select
    from app.core.db import engine
    from app.services.federation import routing, schema as fs
    from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock
    stream, remote = None, None
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        obj = await crypto._crypto_object(path, admin=admin)
        if not admin and (obj["kind"] != "lyric" or obj["encryption"] is None):
            raise HTTPException(404, "Encrypted lyrics not found")
        if expected_id:
            if obj["media_id"] != expected_id or obj["encryption"] is not None or not re.fullmatch(r"[a-f0-9]{64}", snapshot):
                raise HTTPException(409, "Download source changed")
        if obj["resource_id"]:
            row, relation = await routing.resolve(obj["resource_id"], path)
            if expected_id:
                async with engine.connect() as conn:
                    source = (await conn.execute(select(fs.global_media).where(fs.global_media.c.media_id == expected_id))).mappings().one()
                if crypto.download_snapshot(expected_id, path, obj["size_bytes"], crypto._global_revision(dict(source))) != snapshot:
                    raise HTTPException(409, "Download source changed")
            if relation is not None:
                remote = obj["resource_id"]
        if remote is None:
            try:
                stream = crypto.safe_open_media(path)
            except (OSError, ValueError) as exc:
                raise HTTPException(404, "Media object not found") from exc
            if expected_id and not obj["resource_id"]:
                if crypto.download_snapshot(expected_id, path, os.fstat(stream.fileno()).st_size,
                                           crypto._local_revision(os.fstat(stream.fileno()))) != snapshot:
                    stream.close()
                    raise HTTPException(409, "Download source changed")
    if remote is not None:
        response = await routing.stream(remote, path)
        response.headers.update(NO_STORE_HEADERS)
        return response
    return await pinned_bytes(request, obj, stream)


@router.get("/api/v1/media/crypto/bytes")
@router.head("/api/v1/media/crypto/bytes", include_in_schema=False)
async def public_bytes(request: Request, file_path: str):
    await available(request)
    return await raw_bytes(request, file_path, admin=False)


@router.get("/api/v1/media/admin/crypto/bytes")
@router.head("/api/v1/media/admin/crypto/bytes", include_in_schema=False)
async def admin_bytes(request: Request, file_path: str, _binding: str = Depends(admin_binding)):
    await available(request)
    return await raw_bytes(request, file_path, admin=True)


@router.get("/api/v1/media/admin/download/plan")
async def download_plan(request: Request, paths: str, binding: str = Depends(admin_binding)):
    await available(request)
    try:
        selection = json.loads(paths)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "下载参数无效") from exc
    if not isinstance(selection, list) or not selection or len(selection) > settings.ADMIN_MAX_DOWNLOAD_ITEMS:
        raise HTTPException(400, "请选择合法下载对象")
    entries = await crypto.browser_download_plan(selection)
    encrypted = any(item.get("encryption") for item in entries)
    for item in entries:
        if encrypted:
            item["url"] = "/api/v1/media/admin/download/bytes?" + urlencode({"file_path": item["path"], "media_id": item["media_id"], "snapshot": item.pop("snapshot")})
        else:
            item.pop("snapshot")
            item["url"] = "/api/v1/media/admin/download?" + urlencode({"paths": json.dumps([item["path"]], separators=(",", ":"))})
    if encrypted:
        await admin_service.audit(binding.removeprefix("admin:"), "download", len(entries),
                                  json.dumps(selection, ensure_ascii=False), "success", "browser encrypted download plan", request)
    return {"items": entries}


@router.get("/api/v1/media/admin/download/bytes")
@router.head("/api/v1/media/admin/download/bytes", include_in_schema=False)
async def download_bytes(request: Request, file_path: str, media_id: str, snapshot: str, _binding: str = Depends(admin_binding)):
    await available(request)
    return await raw_bytes(request, file_path, admin=True, expected_id=media_id, snapshot=snapshot)


@router.post("/api/v1/karaoke/account/recordings/crypto/prepare")
async def recording_prepare(request: Request, response: Response, body: RecordingPrepareBody):
    from app.api.v1.karaoke_users import _user
    from app.services import recording_crypto
    manager = await available(request)
    same_origin(request)
    user = await _user(request, True)
    if body.plaintext_size > recording_crypto.MAX_ENCRYPTED_LYRIC_PLAINTEXT:
        raise HTTPException(413, "加密歌词快照超过大小限制")
    _source, source_id = await recording_crypto.source_lyric(body.media)
    browser = browser_binding(request, response, create=False)
    descriptor = crypto.Metadata.new(body.plaintext_size)
    preparation = recording_crypto.preparation_binding(user["user_id"], browser, body.media, source_id)
    return {"encryption": descriptor.to_dict(), "key_envelope": manager.wrap(browser, body.session_id, descriptor),
            "preparation_token": manager.sign_preparation(preparation, descriptor)}


@router.post("/api/v1/karaoke/account/recordings/{recording_id}/lyrics-key")
async def recording_key(request: Request, response: Response, recording_id: str, body: RecordingKeyBody):
    from sqlalchemy import select
    from app.api.v1.karaoke_users import _user
    from app.services import recording_crypto, karaoke_schema as ks
    from app.services.federation.state import state
    manager = await available(request)
    same_origin(request)
    user = await _user(request, True)
    async with state.database.connect() as conn:
        row = (await conn.execute(select(ks.recordings).where(ks.recordings.c.recording_id == recording_id,
            ks.recordings.c.user_id == user["user_id"], ks.recordings.c.state == "ready"))).mappings().first()
        if row is None:
            raise HTTPException(404, "录音不存在")
        _lyrics, snapshot, _expected = recording_crypto.decode_lyrics(row["lyrics"])
        if snapshot is None:
            raise HTTPException(409, "该录音没有加密歌词快照")
        descriptor = crypto.Metadata.from_dict(snapshot["encryption"])
        stored = await crypto.encryption_for_id(conn, recording_id, object_kind="recording_lyric")
        if stored != descriptor:
            raise crypto.MetadataError("Recording encrypted snapshot identity does not match its metadata")
    browser = browser_binding(request, response, create=False)
    return {"encrypted_lyrics": snapshot, "key_envelope": manager.wrap(browser, body.session_id, descriptor)}
