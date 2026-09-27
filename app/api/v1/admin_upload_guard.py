"""Keep multipart upload contracts while blocking bypasses and incomplete records."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.api.v1 import admin as legacy_admin
from app.core.db import engine
from app.services import admin_service, media_objects
from app.services.federation.state import state as node_state
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import LYRICS_ROOT, MEDIA_ROOT, MediaManager


router = APIRouter()
logger = logging.getLogger("frontiercloud.admin.upload")


@router.post("/upload/item")
async def upload_item(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    target_dir: Annotated[str, Form()] = "",
    relative_path: Annotated[str | None, Form()] = None,
    session_hash: str = Depends(legacy_admin.require_session),
    site_type: Annotated[str | None, Form()] = None,
):
    source = relative_path or file.filename or ""
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
    )
    if isinstance(result, dict):
        return {**result, "site_type": "primary", "site_label": "主站"}
    return result


def _rollback_unregistered_lyric(saved_path: str) -> None:
    target = (MEDIA_ROOT / saved_path).resolve()
    lyric_root = LYRICS_ROOT.resolve()
    if not target.is_relative_to(lyric_root):
        return
    target.unlink(missing_ok=True)
    parent = target.parent
    while parent != lyric_root:
        try:
            parent.rmdir()
        except (FileNotFoundError, OSError):
            break
        parent = parent.parent


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
    relative_path: Annotated[str | None, Form()] = None,
    session_hash: str = Depends(legacy_admin.require_session),
):
    """Publish an LRC only when its managed-object record commits atomically.

    The physical file is staged/published first. Object registration and the
    success audit then share one MySQL transaction. A failure before that
    transaction commits removes the file; failures in cache invalidation after
    commit never remove a durable, registered lyric.
    """
    source = relative_path or file.filename or ""
    saved_path: str | None = None
    committed = False
    audit = legacy_admin._mutation_audit(session_hash, "upload_lyric", [source], request)
    try:
        try:
            saved_path = await MediaManager.upload_lyric(file, relative_path)
            async with engine.begin() as conn:
                await media_objects.ensure_object(conn, saved_path, "lyric")
                await audit(conn, "success", 1, {"path": saved_path})
            committed = True
        except HTTPException as exc:
            await _failed_upload_audit(audit, {"error": str(exc.detail)})
            raise
        except Exception as exc:
            if saved_path and not committed:
                _rollback_unregistered_lyric(saved_path)
            await _failed_upload_audit(audit, {"error": str(exc)})
            raise HTTPException(
                status_code=500,
                detail="歌词上传未能写入系统记录，已回滚文件",
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
