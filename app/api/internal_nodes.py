"""HTTPS-only V1 control endpoints; independent relationship authentication."""
from __future__ import annotations

import hashlib
import base64
import json
import os
import secrets
import time
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, func, select, text, update

from app.core.client_ip import resolve_client_identity
from app.core.config import settings
from app.services import resource_pool
from app.services.federation import protocol as p
from app.services.federation import schema as s
from app.services.federation.catalog import catalog
from app.services.federation.state import state
from app.services.federation.transport import transport

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


def require_https(request: Request):
    if request is None:
        raise HTTPException(403, "节点资源需要经过 HTTPS 业务入口")
    forwarded = request.headers.get("x-forwarded-proto", "")
    trusted = resolve_client_identity(request.scope).from_trusted_proxy
    if not settings.TLS_ENABLED or not (request.url.scheme == "https" or (trusted and forwarded == "https")):
        raise HTTPException(403, "节点控制面仅允许已启用 TLS 的 HTTPS")


async def control_body(request: Request) -> bytes:
    chunks, length = [], 0
    async for chunk in request.stream():
        length += len(chunk)
        if length > p.MAX_CONTROL_BYTES:
            raise HTTPException(413, "Control message too large")
        chunks.append(chunk)
    return b"".join(chunks)


def control_path(request):
    return request.url.path + ("?" + request.url.query if request.url.query else "")


async def authenticated(request, *, pending=False, revoked=False):
    require_https(request)
    body = await control_body(request)
    request.state.node_control_body = body
    try:
        relation = await state.authenticate(request.headers, request.method, control_path(request), body,
            allow_pending=pending, allow_revoked=revoked)
        return relation
    except p.ProtocolError as exc:
        raise HTTPException(401, str(exc)) from exc


@router.get("/identity")
async def identity(request: Request, challenge: str):
    require_https(request)
    try:
        return JSONResponse(state.identity(challenge), headers={"Cache-Control": "no-store"})
    except p.ProtocolError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/pair")
async def pair(request: Request):
    require_https(request)
    try:
        value = json.loads(await control_body(request))
        private = state.unseal(state.node["private_key"])
        package = p.verify(p.public_key(private), value["package"])
        now = int(time.time())
        if (state.node["role"] != "Follower" or package["node_id"] != state.node["node_id"]
                or package["protocol"] != p.PROTOCOL_VERSION
                or not isinstance(package["expires_at"], int) or isinstance(package["expires_at"], bool)
                or package["expires_at"] <= now or not p.IDENTIFIER.fullmatch(package["nonce"])
                or not p.IDENTIFIER.fullmatch(value["relationship_id"])
                or len(p.decode(value["credential"])) != 48):
            raise p.ProtocolError("Invalid or expired pairing package")
        # Reject retained, consumed or expired packages before outbound I/O.
        # consume() rechecks and reserves the package in its final transaction;
        # no database lock is held while verifying the remote HTTPS identity.
        async with state.database.connect() as conn:
            issued = (await conn.execute(select(s.pairs).where(s.pairs.c.nonce == package["nonce"]))).mappings().first()
        if (not issued or issued["state"] != "issued" or issued["expires_at"] <= now
                or not secrets.compare_digest(issued["token_hash"], p.digest(package["token"]))):
            raise p.ProtocolError("Pairing package unavailable")
        master_payload = value["master"]["payload"]
        master = await transport.identity(master_payload["endpoint"], expected_id=master_payload["node_id"],
            expected_key=master_payload["public_key"], role="Master")
        p.verify(master["public_key"], value["master"])
        await state.consume(package, value["relationship_id"], master, value["credential"])
        return {"state": "pending", "protocol": p.PROTOCOL_VERSION}
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(409, "配对被拒绝：包无效、身份不匹配、已使用或已过期") from exc


@router.post("/confirm")
async def confirm(request: Request):
    relation = await authenticated(request, pending=True)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(409, "Invalid confirmation direction")
    await state.activate(relation["relationship_id"], relation["peer_id"])
    from app.services.federation.runtime import runtime
    runtime.start()
    return {"state": "active", "protocol": p.PROTOCOL_VERSION}


@router.post("/revoke")
async def revoke(request: Request):
    relation = await authenticated(request, pending=True, revoked=True)
    await state.revoke(relation["relationship_id"], relation["peer_id"], peer_confirmed=True)
    from app.services.media_catalog_cache import invalidate_media_catalog
    await invalidate_media_catalog()
    return {"state": "revoked", "protocol": p.PROTOCOL_VERSION}


@router.post("/heartbeat")
async def heartbeat(request: Request):
    relation = await authenticated(request)
    if request is not None:
        try:
            value = json.loads(request.state.node_control_body or b"{}")
            mode = value.get("mode")
            if mode is not None:
                if relation["direction"] != "upstream" or mode not in ("Relay", "Direct"):
                    raise p.ProtocolError("Invalid relationship mode")
                if mode != relation["mode"]:
                    await state.accept_mode(relation["relationship_id"], mode, relation["peer_id"])
            resources = value.get("resources")
            if resources is not None:
                if relation["direction"] != "upstream":
                    raise p.ProtocolError("Invalid resource configuration direction")
                from app.services import resource_pool
                await resource_pool.accept_follower_configuration(resources, state.node, state.database)
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(400, "Invalid heartbeat configuration") from exc
    # Only our own outbound probe establishes peer reachability. Incoming probes
    # must not hide a peer whose HTTPS/media ingress is broken.
    summary = {"app_version": p.APP_VERSION, "protocol": p.PROTOCOL_VERSION}
    if relation["direction"] == "upstream":
        from app.services import resource_pool
        summary.update(await resource_pool.follower_resource_summary(state.node, state.database))
    return summary


@router.post("/backup/begin")
async def backup_begin(request: Request):
    relation = await authenticated(request)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only a Follower stores Master backups")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        generation = int(value["generation"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid backup generation") from exc
    from app.services import resource_pool
    await resource_pool.backup_begin(relation["peer_id"], generation, state.database)
    return {"status": "receiving"}


@router.post("/backup/chunk")
async def backup_chunk(request: Request):
    relation = await authenticated(request)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only a Follower stores Master backups")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        chunk = base64.b64decode(value["chunk"], validate=True)
        generation = int(value["generation"])
        chunk_index = int(value["chunk_index"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid backup chunk") from exc
    from app.services import resource_pool
    await resource_pool.backup_append(relation["peer_id"], generation, chunk_index, chunk, state.database)
    return {"status": "receiving", "bytes": len(chunk)}


@router.post("/backup/commit")
async def backup_commit(request: Request):
    relation = await authenticated(request)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only a Follower stores Master backups")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        generation = int(value["generation"])
        checksum = str(value["checksum"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid backup manifest") from exc
    from app.services import resource_pool
    size = await resource_pool.backup_commit(
        relation["peer_id"], generation, checksum, state.node, state.database,
    )
    return {"status": "ready", "bytes": size}


@router.post("/jobs/lease")
async def job_lease(request: Request):
    relation = await authenticated(request)
    if state.node["role"] != "Master" or relation["direction"] != "downstream":
        raise HTTPException(403, "Only Master leases worker jobs")
    value = json.loads(request.state.node_control_body or b"{}")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, list) or len(capabilities) > 32:
        raise HTTPException(400, "Invalid worker capabilities")
    from app.services import resource_pool
    return {"job": await resource_pool.lease_job(relation["peer_id"], capabilities, state.database)}


@router.post("/jobs/{job_id}/complete")
async def job_complete(request: Request, job_id: str):
    relation = await authenticated(request)
    if state.node["role"] != "Master" or relation["direction"] != "downstream":
        raise HTTPException(403, "Only Master accepts worker results")
    value = json.loads(request.state.node_control_body or b"{}")
    if not isinstance(value.get("result"), dict):
        raise HTTPException(400, "Invalid worker result")
    from app.services import resource_pool
    await resource_pool.complete_job(relation["peer_id"], job_id, str(value.get("lease") or ""),
                                     value["result"], state.database)
    return {"status": "complete"}


async def owned_path(original: str):
    if not p.OBJECT_ID.fullmatch(original):
        raise HTTPException(404, "Resource not found")
    async with state.database.connect() as conn:
        path = (await conn.execute(text("SELECT media_path FROM media_objects WHERE media_id=:id AND object_kind IN ('audio','video')"), {"id": original})).scalar_one_or_none()
    if not path:
        raise HTTPException(404, "Resource not found")
    return path


async def storage_capability(request: Request, original: str, operation: str) -> tuple[dict, dict]:
    require_https(request)
    token = request.headers.get("x-storage-capability") or request.query_params.get("token", "")
    try:
        hint = json.loads(p.decode(token.split(".", 1)[0]))
        relation = await state.relationship(hint["r"])
        value = p.verify_storage_token(state.unseal(relation["credential"]), token, int(time.time()))
        if (state.node["role"] != "Follower" or relation["direction"] != "upstream"
                or relation["state"] != "active" or value["m"] != relation["peer_id"]
                or value["n"] != state.node["node_id"] or value["i"] != original
                or value["op"] != operation):
            raise p.ProtocolError("Storage relationship mismatch")
        from app.services.resource_pool import validate_media_path
        validate_media_path(value["path"])
        return relation, value
    except (KeyError, TypeError, ValueError, p.ProtocolError) as exc:
        raise HTTPException(401, "Storage capability invalid or expired") from exc


def storage_cors(relation: dict, origin: str | None) -> dict[str, str]:
    headers = {"Cache-Control": "no-store", "Vary": "Origin"}
    if origin:
        if origin != relation["peer_endpoint"]:
            raise HTTPException(403, "Unpaired storage origin")
        headers.update({"Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "PUT, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type, X-Storage-Capability"})
    return headers


@router.options("/storage/{original}")
async def storage_preflight(request: Request, original: str):
    require_https(request)
    origin = request.headers.get("origin")
    relation = next((row for row in await state.list_relationships()
                     if row["direction"] == "upstream" and row["state"] == "active"
                     and row["peer_endpoint"] == origin), None)
    if not relation:
        raise HTTPException(403, "Unpaired storage origin")
    return Response(headers=storage_cors(relation, origin))


@router.put("/storage/{original}")
async def storage_upload(request: Request, original: str):
    from app.api.v1.media import MEDIA_ROOT
    from app.services.media_manager import MediaManager, SIGNATURES
    relation, value = await storage_capability(request, original, "upload")
    expected = int(value["size"])
    target = (MEDIA_ROOT / value["path"]).resolve()
    if MEDIA_ROOT not in target.parents or target.exists():
        raise HTTPException(409, "Storage target already exists or is invalid")
    if expected <= 0:
        raise HTTPException(413, "Upload size is invalid")
    reserved = False
    async with state.database.begin() as conn:
        member = (await conn.execute(select(s.storage_members).where(
            s.storage_members.c.member_id == state.node["node_id"]).with_for_update())).mappings().first()
        physical_available = resource_pool.physical_free(MEDIA_ROOT) - resource_pool.PHYSICAL_RESERVE_BYTES
        logical_available = (int(member["allocated_bytes"]) - int(member["used_bytes"])
                             - int(member["reserved_bytes"])) if member else 0
        if (not member or not member["storage_enabled"] or not member["writable"]
                or min(physical_available, logical_available) < expected):
            raise HTTPException(507, "Follower storage is not writable or lacks capacity")
        await conn.execute(update(s.storage_members).where(
            s.storage_members.c.member_id == state.node["node_id"]
        ).values(reserved_bytes=s.storage_members.c.reserved_bytes + expected,
                 physical_free_bytes=resource_pool.physical_free(MEDIA_ROOT), updated_at=int(time.time())))
        reserved = True
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = (MEDIA_ROOT / f".cluster-upload-{original}.part").resolve()
    written, digest, head = 0, hashlib.sha256(), b""
    try:
        with temporary.open("xb") as output:
            async for chunk in request.stream():
                if not head:
                    head = chunk[:4096]
                written += len(chunk)
                if written > expected:
                    raise HTTPException(413, "Upload exceeds reserved size")
                digest.update(chunk); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        if written != expected:
            raise HTTPException(400, "Upload size does not match reservation")
        extension = target.suffix.lower()
        if not SIGNATURES.get(extension, lambda _data: False)(head):
            raise HTTPException(400, "媒体内容与扩展名不匹配")
        MediaManager._validate_media_destination(target)
        os.replace(temporary, target)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with state.database.begin() as conn:
            await conn.execute(text("""
                INSERT INTO media_objects
                (media_id, object_kind, media_path, path_locator, created_at, updated_at)
                VALUES (:media_id, :kind, :path, :locator, :now, :now)
                ON DUPLICATE KEY UPDATE media_path=VALUES(media_path), updated_at=VALUES(updated_at)
            """), {"media_id": original, "kind": "audio" if value["path"].startswith("music/") else "video",
                     "path": value["path"], "locator": hashlib.sha256(value["path"].encode()).hexdigest(), "now": now})
            await conn.execute(update(s.storage_members).where(
                s.storage_members.c.member_id == state.node["node_id"]
            ).values(reserved_bytes=func.greatest(0, s.storage_members.c.reserved_bytes - expected),
                     used_bytes=s.storage_members.c.used_bytes + written,
                     physical_free_bytes=resource_pool.physical_free(MEDIA_ROOT), updated_at=int(time.time())))
            reserved = False
        return JSONResponse({"object_id": original, "size_bytes": written,
                             "sha256": digest.hexdigest(), "etag": f'"{digest.hexdigest()}"'},
                            headers=storage_cors(relation, request.headers.get("origin")))
    except BaseException:
        temporary.unlink(missing_ok=True)
        if reserved:
            async with state.database.begin() as conn:
                await conn.execute(update(s.storage_members).where(
                    s.storage_members.c.member_id == state.node["node_id"]
                ).values(reserved_bytes=func.greatest(
                    0, s.storage_members.c.reserved_bytes - expected), updated_at=int(time.time())))
        raise


@router.post("/storage/{original}/stat")
async def storage_stat(request: Request, original: str):
    relation = await authenticated(request)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only a Follower reports storage objects")
    value = json.loads(request.state.node_control_body or b"{}")
    if value.get("path") != await owned_path(original):
        raise HTTPException(404, "Storage object not found")
    from app.api.v1.media import MEDIA_ROOT
    target = (MEDIA_ROOT / value["path"]).resolve()
    digest = hashlib.sha256()
    with target.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"object_id": original, "size_bytes": target.stat().st_size,
            "sha256": digest.hexdigest(), "etag": f'"{digest.hexdigest()}"'}


@router.post("/storage/{original}/delete")
async def storage_delete(request: Request, original: str):
    _relation, value = await storage_capability(request, original, "delete")
    from app.api.v1.media import MEDIA_ROOT
    target = (MEDIA_ROOT / value["path"]).resolve()
    if MEDIA_ROOT not in target.parents:
        raise HTTPException(404, "Storage object not found")
    removed = target.stat().st_size if target.is_file() else 0
    target.unlink(missing_ok=True)
    async with state.database.begin() as conn:
        await conn.execute(text("DELETE FROM media_objects WHERE media_id=:media_id"), {"media_id": original})
        if removed:
            await conn.execute(update(s.storage_members).where(
                s.storage_members.c.member_id == state.node["node_id"]
            ).values(used_bytes=func.greatest(0, s.storage_members.c.used_bytes - removed),
                     physical_free_bytes=resource_pool.physical_free(MEDIA_ROOT), updated_at=int(time.time())))
    return {"status": "deleted"}


@router.api_route("/media/{original}", methods=["GET", "HEAD", "OPTIONS"])
async def owned_media(request: Request, original: str, token: str | None = None):
    require_https(request)
    try:
        header_token = request.headers.get("x-media-capability")
        if token and header_token and token != header_token:
            raise p.ProtocolError("Conflicting media capabilities")
        token = token or header_token
        if not isinstance(token, str):
            raise p.ProtocolError("Missing media capability")
        # Decode only the lookup ID, then authenticate the entire capability.
        hint = json.loads(p.decode(token.split(".", 1)[0]))
        relation = await state.relationship(hint["r"])
        payload = p.verify_media_token(state.unseal(relation["credential"]), token, int(time.time()))
        if (state.node["role"] != "Follower" or relation["state"] != "active" or relation["direction"] != "upstream"
                or payload["o"] != state.node["node_id"] or payload["i"] != original or payload["m"] != relation["peer_id"]):
            raise p.ProtocolError("Media relationship mismatch")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(401, "Media capability invalid or expired") from exc
    resource_id = payload["g"]
    request.scope["media_audit"] = {
        "resource_id": resource_id, "owner_id": payload["o"], "media_id": original,
        "parent_request_id": payload.get("request_id", ""), "trace_id": payload.get("trace_id", ""),
    }
    origin = request.headers.get("origin")
    cors = {"Cache-Control": "no-store", "Vary": "Origin"}
    if origin:
        if origin != relation["peer_endpoint"]:
            raise HTTPException(403, "Unpaired media origin")
        cors.update({"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                     "Access-Control-Allow-Headers": "Range, If-Range, If-None-Match, If-Modified-Since",
                     "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, ETag, Last-Modified"})
    if request.method == "OPTIONS":
        return Response(headers=cors)
    from app.api.v1.media import stream_media_file
    response = await stream_media_file(await owned_path(original))
    response.headers.update(cors)
    response.headers.update({
        "X-Media-Resource-ID": resource_id, "X-Media-Owner-ID": payload["o"], "X-Media-Object-ID": original,
        "X-Media-Parent-Request-ID": payload.get("request_id", ""), "X-Audit-Trace-ID": payload.get("trace_id", ""),
    })
    return response


def _recording_cors(relation: dict, origin: str | None) -> dict[str, str]:
    headers = {"Cache-Control": "no-store", "Vary": "Origin"}
    if origin:
        if origin != relation["peer_endpoint"]:
            raise HTTPException(403, "Unpaired recording origin")
        headers.update({
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, HEAD, PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Recording-Capability",
            "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, ETag, Last-Modified",
        })
    return headers


@router.options("/recordings/{recording_id}")
async def recording_preflight(request: Request, recording_id: str):
    require_https(request)
    origin = request.headers.get("origin")
    relations = await state.list_relationships()
    relation = next((row for row in relations if row["direction"] == "upstream"
                     and row["state"] == "active" and row["peer_endpoint"] == origin), None)
    if relation is None:
        raise HTTPException(403, "Unpaired recording origin")
    return Response(headers=_recording_cors(relation, origin))


@router.api_route("/recordings/{recording_id}", methods=["PUT", "GET", "HEAD"])
async def recording_bytes(request: Request, recording_id: str):
    from app.services import karaoke_storage
    require_https(request)
    token = request.headers.get("x-recording-capability") or request.query_params.get("token", "")
    operation = "upload" if request.method == "PUT" else "read"
    relation, value = await karaoke_storage.capability(token, operation, recording_id)
    cors = _recording_cors(relation, request.headers.get("origin"))
    if request.method == "PUT":
        result = await karaoke_storage.receive(request, relation, value)
        return JSONResponse(result, headers=cors)
    redirect = karaoke_storage.protected_redirect(
        relation["relationship_id"], value["u"], recording_id
    )
    disposition = ("attachment" if value["op"] == "download" else "inline")
    return Response(headers={**cors, "X-Accel-Redirect": redirect,
                             "Content-Type": value["ct"],
                             "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(value['name'])}"})


@router.post("/recordings/{recording_id}/stat")
async def recording_stat(request: Request, recording_id: str):
    from app.services import karaoke_storage
    relation = await authenticated(request)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only a Follower stores recordings")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        return karaoke_storage.stat(relation["relationship_id"], value["user_id"], recording_id)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, "Invalid recording stat request") from exc


@router.post("/recordings/{recording_id}/delete")
async def recording_delete(request: Request, recording_id: str):
    from app.services import karaoke_storage
    relation = await authenticated(request)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only a Follower stores recordings")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        removed = karaoke_storage.remove(relation["relationship_id"], value["user_id"], recording_id)
        if removed:
            async with state.database.begin() as conn:
                await conn.execute(update(s.storage_members).where(
                    s.storage_members.c.member_id == state.node["node_id"]
                ).values(used_bytes=func.greatest(0, s.storage_members.c.used_bytes - removed),
                         updated_at=int(time.time())))
        return {"status": "deleted"}
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, "Invalid recording delete request") from exc


@router.post("/recordings/users/{user_id}/delete")
async def recording_user_delete(request: Request, user_id: str):
    from app.services import karaoke_storage
    relation = await authenticated(request)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only a Follower stores recordings")
    removed = karaoke_storage.remove_user(relation["relationship_id"], user_id)
    if removed:
        async with state.database.begin() as conn:
            await conn.execute(update(s.storage_members).where(
                s.storage_members.c.member_id == state.node["node_id"]
            ).values(used_bytes=func.greatest(0, s.storage_members.c.used_bytes - removed),
                     updated_at=int(time.time())))
    return {"status": "deleted", "removed_bytes": removed}
