"""Keep multipart upload contracts while blocking bypasses and incomplete records."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.api.v1 import admin as legacy_admin
from app.services import admin_service, media_objects
from app.services.federation.state import state as node_state
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import LYRICS_ROOT, MEDIA_ROOT, MediaManager


router = APIRouter()


@router.post("/upload/item")
async def upload_item(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    target_dir: Annotated[str, Form()] = "",
    relative_path: Annotated[str | None, Form()] = None,
    session_hash: str = Depends(legacy_admin.require_session),
):
    if node_state.node["role"] == "Master":
        source = relative_path or file.filename or ""
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
            detail="Master 媒体上传必须通过 Storage Pool；请刷新管理页后重试",
        )
    return await legacy_admin.upload_item(
        request,
        file,
        target_dir,
        relative_path,
        session_hash,
    )


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


@router.post("/upload/lyric")
async def upload_lyric(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    relative_path: Annotated[str | None, Form()] = None,
    session_hash: str = Depends(legacy_admin.require_session),
):
    """Persist every accepted LRC as a managed lyric object before success."""
    source = relative_path or file.filename or ""
    saved_path: str | None = None
    audit = legacy_admin._mutation_audit(session_hash, "upload_lyric", [source], request)
    try:
        try:
            saved_path = await MediaManager.upload_lyric(file, relative_path)
            await media_objects.ensure_objects([(saved_path, "lyric")])
            await invalidate_media_catalog()
            await audit(None, "success", 1, {"path": saved_path})
            return {"path": saved_path}
        except HTTPException as exc:
            await audit(None, "failed", 1, {"error": str(exc.detail)})
            raise
        except Exception as exc:
            if saved_path:
                _rollback_unregistered_lyric(saved_path)
                await invalidate_media_catalog()
            await audit(None, "failed", 1, {"error": str(exc)})
            raise HTTPException(
                status_code=500,
                detail="歌词上传未能写入系统记录，已回滚文件",
            ) from exc
    finally:
        await file.close()
