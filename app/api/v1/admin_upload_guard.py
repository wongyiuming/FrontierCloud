"""Keep the legacy multipart contract while blocking Master filesystem bypasses."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.api.v1 import admin as legacy_admin
from app.services import admin_service
from app.services.federation.state import state as node_state


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
