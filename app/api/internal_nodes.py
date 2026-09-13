"""HTTPS-only V1 control endpoints; independent relationship authentication."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select, text

from app.core.client_ip import resolve_client_identity
from app.core.config import settings
from app.services.federation import protocol as p
from app.services.federation import schema as s
from app.services.federation.catalog import catalog
from app.services.federation.state import state
from app.services.federation.transport import transport

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


def require_https(request: Request):
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
    if state.node["role"] != "Slave" or relation["direction"] != "upstream":
        raise HTTPException(409, "Invalid confirmation direction")
    await state.activate(relation["relationship_id"], relation["peer_id"])
    from app.services.federation.runtime import runtime
    runtime.start()
    return {"state": "active", "protocol": p.PROTOCOL_VERSION}


@router.post("/revoke")
async def revoke(request: Request):
    relation = await authenticated(request, pending=True, revoked=True)
    if relation["state"] != "revoked":
        await state.revoke(relation["relationship_id"], relation["peer_id"])
    return {"state": "revoked", "protocol": p.PROTOCOL_VERSION}


@router.post("/heartbeat")
async def heartbeat(request: Request):
    relation = await authenticated(request)
    summary = await catalog.summary()
    # Successful authenticated ingress is liveness evidence for this one peer.
    await state.heartbeat(relation["relationship_id"], True, summary=relation["summary"])
    return summary


@router.get("/catalog")
async def catalog_page(request: Request, cursor: int = 0, head: int | None = None):
    relation = await authenticated(request)
    if state.node["role"] != "Slave" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only a Slave exports its owned catalog")
    try:
        return await catalog.page(cursor, head)
    except p.ProtocolError as exc:
        raise HTTPException(409, str(exc)) from exc


async def owned_path(original: str):
    if not p.OBJECT_ID.fullmatch(original):
        raise HTTPException(404, "Resource not found")
    async with state.database.connect() as conn:
        path = (await conn.execute(text("SELECT media_path FROM media_objects WHERE media_id=:id AND object_kind IN ('audio','video')"), {"id": original})).scalar_one_or_none()
    if not path:
        raise HTTPException(404, "Resource not found")
    return path


@router.get("/lyrics/{original}")
async def owned_lyrics(request: Request, original: str):
    relation = await authenticated(request)
    if state.node["role"] != "Slave" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only the media owner resolves attachments")
    from app.api.v1.media import get_lyrics_content
    return await get_lyrics_content(await owned_path(original))


@router.api_route("/media/{original}", methods=["GET", "HEAD", "OPTIONS"])
async def owned_media(request: Request, original: str, token: str):
    require_https(request)
    try:
        # Decode only the lookup ID, then authenticate the entire capability.
        hint = json.loads(p.decode(token.split(".", 1)[0]))
        relation = await state.relationship(hint["r"])
        payload = p.verify_media_token(state.unseal(relation["credential"]), token, int(time.time()))
        if (state.node["role"] != "Slave" or relation["state"] != "active" or relation["direction"] != "upstream"
                or payload["o"] != state.node["node_id"] or payload["i"] != original or payload["m"] != relation["peer_id"]):
            raise p.ProtocolError("Media relationship mismatch")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(401, "Media capability invalid or expired") from exc
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
    return response
