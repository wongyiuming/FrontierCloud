"""Keep multipart upload contracts while blocking bypasses and incomplete records."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.api.v1 import admin as legacy_admin
from app.core.db import engine
from app.services import admin_service, media_objects, media_crypto
from app.services import media_publication_recovery as publication
from app.services.federation.state import state as node_state
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import LYRICS_ROOT, MEDIA_ROOT, MediaManager


router = APIRouter()
logger = logging.getLogger("frontiercloud.admin.upload")


@router.post("/upload/item")
async def upload_item(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    storage_mode: Annotated[str, Form(pattern="^(encrypted|plain)$")],
    encryption: Annotated[str | None, Form()] = None,
    preparation_token: Annotated[str, Form()] = "",
    target_dir: Annotated[str, Form()] = "",
    relative_path: Annotated[str | None, Form()] = None,
    session_hash: str = Depends(legacy_admin.require_session),
    site_type: Annotated[str | None, Form()] = None,
):
    source = relative_path or file.filename or ""
    if node_state.node.get("role") == "Follower":
        await file.close()
        raise HTTPException(409, "Storage nodes cannot accept business uploads")
    try:
        metadata = media_crypto.upload_encryption(storage_mode, encryption, preparation_token, "admin:" + session_hash)
    except media_crypto.MetadataError as exc:
        await file.close()
        raise HTTPException(400, str(exc)) from exc
    if node_state.node["role"] == "Master":
        try:
            await admin_service.audit(
                session_hash,
                "upload_item",
                1,
                source,
                "failed",
                "Master requires Storage Pool upload",
                request,
            )
        finally:
            await file.close()
        raise HTTPException(
            status_code=409,
            detail="Master 媒体上传必须通过站点类型路由；请刷新管理页后重试",
        )
    if str(site_type or "").strip().lower() != "primary":
        await file.close()
        raise HTTPException(status_code=400, detail="Standalone 媒体上传必须选择主站")
    result = await legacy_admin.upload_item(
        request,
        file,
        target_dir,
        relative_path,
        session_hash,
        encryption=metadata,
    )
    if isinstance(result, dict):
        return {**result, "site_type": "primary", "site_label": "主站"}
    return result


async def _register_lyric_publication(saved_path, journal, metadata, audit):
    if saved_path != journal['path'] or (metadata.to_dict() if metadata else None) != journal['encryption']:
        raise RuntimeError('Lyric publication identity changed')
    await publication.register(engine, journal, audit)


async def _failed_upload_audit(audit, detail: dict) -> None:
    """Failure evidence is best-effort and must not replace the real upload error."""
    try:
        await audit(None, "failed", 1, detail)
    except Exception as exc:  # pragma: no cover - logging is the fallback itself
        logger.warning("Could not persist lyric upload failure audit: %s", type(exc).__name__)


@router.post("/upload/lyric")
async def upload_lyric(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    storage_mode: Annotated[str, Form(pattern="^(encrypted|plain)$")],
    encryption: Annotated[str | None, Form()] = None,
    preparation_token: Annotated[str, Form()] = "",
    relative_path: Annotated[str | None, Form()] = None,
    session_hash: str = Depends(legacy_admin.require_session),
):
    """Publish an LRC only when its managed-object record commits atomically.

    The staged file, object registration, encryption descriptor, and the
    success audit share one database transaction under the publication fence. A failure before that
    transaction commits removes the file; failures in cache invalidation after
    commit never remove a durable, registered lyric.
    """
    source = relative_path or file.filename or ""
    if node_state.node.get("role") == "Follower":
        await file.close()
        raise HTTPException(409, "Storage nodes cannot accept business uploads")
    try:
        metadata = media_crypto.upload_encryption(storage_mode, encryption, preparation_token, "admin:" + session_hash)
    except media_crypto.MetadataError as exc:
        await file.close()
        raise HTTPException(400, str(exc)) from exc
    saved_path: str | None = None
    audit = legacy_admin._mutation_audit(session_hash, "upload_lyric", [source], request)
    async def register(path, journal):
        await _register_lyric_publication(path, journal, metadata, audit)
    try:
        try:
            saved_path = await MediaManager.upload_lyric(file, relative_path, audit=audit,
                                                        encryption=metadata, registration=register)
        except HTTPException as exc:
            await _failed_upload_audit(audit, {"error": str(exc.detail)})
            raise
        except Exception as exc:
            await _failed_upload_audit(audit, {"error": str(exc)})
            raise HTTPException(
                status_code=500,
                detail="歌词发表未完成；已安全回滚或保留事务日志等待恢复",
            ) from exc

        try:
            await invalidate_media_catalog()
        except Exception as exc:
            logger.warning(
                "Lyric upload committed but catalog invalidation was deferred: %s",
                type(exc).__name__,
                extra={"context": {"path": saved_path}},
            )
        return {"path": saved_path}
    finally:
        await file.close()
