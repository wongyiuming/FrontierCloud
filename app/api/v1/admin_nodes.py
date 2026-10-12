"""Node administration stays on the existing Admin URL and session/CSRF rules."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import text

from app.api.internal_nodes import require_https
from app.api.v1.admin import require_session
from app.services import control_audit, lyrics
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
    certificate = await _certificate_view()
    for relation in relations:
        # Queries are deliberately serial and use the existing authenticated
        # control path. This never asks the Master to read a Storage private key.
        if (state.node.get('role') == 'Master'
                and relation.get('direction') == 'downstream'
                and relation['state'] == 'active'):
            try:
                view = await runtime.call(relation, '/internal/v1/certificate', {'action': 'status'})
            except Exception:
                view = {'state': 'FAILED', 'enabled': True, 'controller_running': False,
                        'https_ready': False, 'serving_matches': False, 'last_error': 'Authenticated certificate observation unavailable'}
            relation['summary'] = {**(relation.get('summary') or {}), 'certificate': view}
    from app.services import storage_capacity
    return {"node_id": state.node["node_id"], "role": state.node["role"], "endpoint": state.node["endpoint"],
            "app_version": p.APP_VERSION, "protocol": p.PROTOCOL_VERSION,
            "certificate": certificate,
            "relationships": [{key: value for key, value in row.items() if key not in ("credential", "peer_key")} for row in relations],
            "storage_pool": await storage_capacity.observed_pool(state) if state.node["role"] == "Master" else None}


async def _certificate_view():
    from app.core.config import settings
    from app.services.certmanager import certificate_control
    if not settings.TLS_ENABLED:
        return {'enabled': False, 'state': 'DISABLED', 'controller_running': False,
                'https_ready': False, 'serving_matches': False, 'managed': False, 'auto_renew': False}
    try:
        return await certificate_control()
    except Exception:
        return {'enabled': True, 'hostname': settings.SERVER_NAME, 'state': 'FAILED',
                'controller_running': False, 'https_ready': False, 'serving_matches': False,
                'managed': True, 'auto_renew': True, 'last_error': 'Automatic certificate controller unavailable'}


class CertificateDownload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str = Field(pattern='^download$')
    browser_public_key: str = Field(min_length=1, max_length=256)
    browser_nonce: str = Field(min_length=1, max_length=256)


async def _node_certificate(identifier, value):
    from app.services.certmanager import certificate_control
    if identifier == state.node['node_id']:
        return await certificate_control(**value)
    if state.node['role'] != 'Master':
        raise HTTPException(403, 'Only the Master can observe a paired Storage certificate')
    relation = await state.relationship(identifier)
    if relation['state'] != 'active' or relation['direction'] != 'downstream':
        raise HTTPException(404, 'Active Storage relationship not found')
    return await runtime.call(relation, '/internal/v1/certificate', value)


@router.get('/{identifier}/certificate')
async def certificate_status(identifier: str, actor: str = Depends(require_session)):
    try:
        return await _node_certificate(identifier, {'action': 'status'})
    except HTTPException:
        raise
    except Exception as exc:
        raise checked(exc) from exc


@router.post('/{identifier}/certificate')
async def certificate_download(identifier: str, payload: CertificateDownload, request: Request,
                               actor: str = Depends(require_session)):
    require_https(request)
    try:
        async with control_audit.action(actor, 'certificate_download', request, source_summary=identifier):
            return await _node_certificate(identifier, payload.model_dump())
    except HTTPException:
        raise
    except Exception as exc:
        raise checked(exc) from exc


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
            compute_enabled=False, worker_slots=0,
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
