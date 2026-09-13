"""Node administration stays on the existing Admin URL and session/CSRF rules."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.internal_nodes import require_https
from app.api.v1.admin import require_session
from app.services.federation import protocol as p
from app.services.federation.catalog import catalog
from app.services.federation.runtime import runtime
from app.services.federation.state import state
from app.services.federation.transport import transport

router = APIRouter(prefix="/nodes")


class Promotion(BaseModel):
    role: str = Field(pattern="^(Master|Slave)$")
    endpoint: str = Field(max_length=512)


class Reinitialization(BaseModel):
    confirmation: str = Field(max_length=32)


class PairImport(BaseModel):
    package: dict


class Mode(BaseModel):
    mode: str = Field(pattern="^(Relay|Direct)$")


def checked(exc):
    return HTTPException(409, str(exc) if isinstance(exc, p.ProtocolError) else "节点 HTTPS 验证或通信失败；请检查证书、网络及节点身份")


@router.get("")
async def status(actor: str = Depends(require_session)):
    relations = await state.list_relationships()
    return {"node_id": state.node["node_id"], "role": state.node["role"], "endpoint": state.node["endpoint"],
            "app_version": p.APP_VERSION, "protocol": p.PROTOCOL_VERSION,
            "relationships": [{key: value for key, value in row.items() if key not in ("credential", "peer_key")} for row in relations]}


@router.post("/promote")
async def promote(request: Request, payload: Promotion, actor: str = Depends(require_session)):
    require_https(request)
    try:
        await transport.identity(payload.endpoint, expected_id=state.node["node_id"], role="Standalone")
        await state.promote(payload.role, payload.endpoint, actor)
        runtime.start()
        return {"role": state.node["role"]}
    except Exception as exc:
        raise checked(exc) from exc


@router.post("/reinitialize")
async def reinitialize(request: Request, payload: Reinitialization, actor: str = Depends(require_session)):
    require_https(request)
    try:
        relations = await state.list_relationships()
        await state.reset(actor, payload.confirmation)
        for relation in relations:
            try:
                await runtime.notify_revocation(relation)
            except Exception:
                pass  # Local trust is already revoked; old capabilities have bounded expiry.
        await runtime.stop()
        runtime.start(revocations=bool(relations))
        from app.services.media_catalog_cache import invalidate_media_catalog
        await invalidate_media_catalog()
        return {"role": "Standalone", "node_id": state.node["node_id"]}
    except Exception as exc:
        raise checked(exc) from exc


@router.post("/pair-package")
async def pairing_package(request: Request, actor: str = Depends(require_session)):
    require_https(request)
    try:
        return await state.create_pair(actor)
    except Exception as exc:
        raise checked(exc) from exc


@router.post("/pair")
async def import_package(request: Request, payload: PairImport, actor: str = Depends(require_session)):
    require_https(request)
    if len(json.dumps(payload.package)) > p.MAX_CONTROL_BYTES:
        raise HTTPException(413, "Pair package too large")
    try:
        identifier = await runtime.import_pair(payload.package, actor)
        return {"relationship_id": identifier}
    except Exception as exc:
        runtime.start()
        raise checked(exc) from exc


@router.post("/{identifier}/mode")
async def change_mode(request: Request, identifier: str, payload: Mode, actor: str = Depends(require_session)):
    require_https(request)
    try:
        await state.set_mode(identifier, payload.mode, actor)
        return {"mode": payload.mode}
    except Exception as exc:
        raise checked(exc) from exc


@router.post("/{identifier}/revoke")
async def revoke(request: Request, identifier: str, actor: str = Depends(require_session)):
    require_https(request)
    try:
        await runtime.revoke(identifier, actor)
        from app.services.media_catalog_cache import invalidate_media_catalog
        await invalidate_media_catalog()
        return {"state": "revoked"}
    except Exception as exc:
        raise checked(exc) from exc


@router.post("/{identifier}/sync")
async def repair(request: Request, identifier: str, actor: str = Depends(require_session)):
    require_https(request)
    try:
        relation = await state.relationship(identifier)
        if state.node["role"] != "Master" or relation["direction"] != "downstream" or relation["state"] != "active":
            raise p.ProtocolError("Only Master repairs an active downstream catalog")
        from sqlalchemy import update
        from app.services.federation import schema as s
        async with state.database.begin() as conn:
            await state.lock(conn)
            await conn.execute(update(s.relationships).where(s.relationships.c.relationship_id == identifier).values(cursor=0))
            await state.log(conn, "catalog-repair", actor, identifier)
        runtime.start()
        return {"state": "repair-scheduled"}
    except Exception as exc:
        raise checked(exc) from exc


@router.get("/{identifier}/resources")
async def test_resources(identifier: str, actor: str = Depends(require_session)):
    relation = await state.relationship(identifier)
    if state.node["role"] != "Master" or relation["direction"] != "downstream":
        raise HTTPException(409, "Only Master tests downstream resources")
    rows = [row for row in await catalog.resources() if row["relationship_id"] == identifier]
    return {"mode": relation["mode"], "items": [{"resource_id": row["resource_id"], "path": row["path"],
        "size": row["payload"]["size"], "url": "/api/v1/media/stream?resource_id=" + row["resource_id"]} for row in rows]}
