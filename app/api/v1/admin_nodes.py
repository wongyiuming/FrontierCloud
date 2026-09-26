"""Node administration stays on the existing Admin URL and session/CSRF rules."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.internal_nodes import require_https
from app.api.v1.admin import require_session
from app.services import control_audit, lyrics, release_control, site_control
from app.services.federation import protocol as p
from app.services.federation.runtime import runtime
from app.services.federation.state import state
from app.services.federation.transport import transport

router = APIRouter(prefix="/nodes")


class Promotion(BaseModel):
    role: str = Field(pattern="^(Master|Follower)$")
    endpoint: str = Field(max_length=512)
    local_capacity_gib: int | None = Field(None, ge=1, le=10240)


class Reinitialization(BaseModel):
    confirmation: str = Field(max_length=32)


class PairImport(BaseModel):
    package: dict


class Mode(BaseModel):
    mode: str = Field(pattern="^(Relay|Direct)$")


class ResourceSettings(BaseModel):
    storage_enabled: bool = False
    storage_capacity_gib: int = Field(ge=0, le=10240)
    compute_enabled: bool = False
    worker_slots: int = Field(ge=0, le=256)
    backup_enabled: bool = False


def checked(exc):
    return HTTPException(409, str(exc) if isinstance(exc, p.ProtocolError) else "节点 HTTPS 验证或通信失败；请检查证书、网络及节点身份")


async def ensure_follower_business_empty() -> None:
    """Ignore only the immutable system fallback lyric when checking business emptiness."""
    from app.api.v1.media import MEDIA_ROOT

    for root_name in ("music", "vido", "lyrics"):
        root = MEDIA_ROOT / root_name
        if not root.exists():
            continue
        for item in root.rglob("*"):
            if not item.is_file():
                continue
            relative = item.relative_to(MEDIA_ROOT).as_posix()
            if relative == lyrics.DEFAULT_LYRIC_PATH and not item.is_symlink():
                continue
            raise p.ProtocolError("Standalone 仍有媒体或歌词；请先执行显式纳管迁移，不能直接固定为 Follower")

    async with state.database.connect() as conn:
        for table in ("karaoke_users", "karaoke_recordings"):
            if int(await conn.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0):
                raise p.ProtocolError("Standalone 仍有 卡拉OK业务数据；请先迁移，不能直接固定为 Follower")


@router.get("")
async def status(actor: str = Depends(require_session)):
    relations = await state.list_relationships()
    from app.services import resource_pool
    return {"node_id": state.node["node_id"], "role": state.node["role"], "endpoint": state.node["endpoint"],
            "app_version": p.APP_VERSION, "protocol": p.PROTOCOL_VERSION,
            "relationships": [{key: value for key, value in row.items() if key not in ("credential", "peer_key")} for row in relations],
            "storage_pool": await resource_pool.pool_summary(state.database) if state.node["role"] == "Master" else None}


@router.get("/release")
async def release_status(refresh_ci: bool = False, actor: str = Depends(require_session)):
    return await release_control.release_status(refresh_ci=refresh_ci)


@router.post("/release/upgrade")
async def release_upgrade(request: Request, actor: str = Depends(require_session)):
    require_https(request)
    try:
        async with control_audit.action(
            actor, "release_upgrade", request,
            source_summary="release_branch=main",
        ):
            site_control.prepare_release()
            return await release_control.start_upgrade()
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/release/rollback")
async def release_rollback(request: Request, actor: str = Depends(require_session)):
    require_https(request)
    try:
        async with control_audit.action(
            actor, "release_rollback", request,
            source_summary="release_branch=main",
        ):
            site_control.prepare_release()
            return await release_control.start_rollback()
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/promote")
async def promote(request: Request, payload: Promotion, actor: str = Depends(require_session)):
    require_https(request)
    try:
        await transport.identity(payload.endpoint, expected_id=state.node["node_id"], role="Standalone")
        if payload.role == "Follower":
            await ensure_follower_business_empty()
        await state.promote(payload.role, payload.endpoint, actor,
                            payload.local_capacity_gib * 1024 ** 3 if payload.local_capacity_gib else None)
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
                pass
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


@router.post("/{identifier}/resources")
async def resource_settings(request: Request, identifier: str, payload: ResourceSettings,
                            actor: str = Depends(require_session)):
    require_https(request)
    try:
        from app.services import resource_pool
        relation = None if identifier == state.node["node_id"] else await state.relationship(identifier)
        await resource_pool.configure_member(
            state.node["node_id"] if relation is None else relation["peer_id"],
            storage_enabled=payload.storage_enabled,
            allocated_bytes=payload.storage_capacity_gib * 1024 ** 3 if payload.storage_enabled else 0,
            compute_enabled=payload.compute_enabled, worker_slots=payload.worker_slots,
            backup_enabled=payload.backup_enabled, actor=actor, store=state,
        )
        runtime.wakeup.set()
        return {"status": "ok"}
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
