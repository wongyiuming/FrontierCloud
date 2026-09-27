"""Admin endpoints for media-directory priority and rename."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.v1.admin import require_session
from app.services import admin_service, media_directories
from app.services.federation.catalog import catalog as node_catalog
from app.services.federation.state import state as node_state


router = APIRouter()


class DirectoryPriorityChange(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    value: int = Field(ge=media_directories.MIN_PREFERENCE, le=media_directories.MAX_PREFERENCE)


class DirectoryRename(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    new_name: str = Field(min_length=1, max_length=255)


async def _require_existing_directory(path: str) -> str:
    try:
        normalized = media_directories.normalize_directory_path(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    role = node_state.node.get("role", "Standalone")
    if role == "Follower":
        raise HTTPException(409, "Follower 的媒体目录由 Master 统一管理")
    if role == "Master":
        if not await node_catalog.resources(directory=normalized):
            raise HTTPException(404, "文件夹不存在或没有托管媒体")
        return normalized
    target = (media_directories.MEDIA_ROOT / normalized).resolve()
    if media_directories.MEDIA_ROOT not in target.parents or not target.is_dir() or target.is_symlink():
        raise HTTPException(404, "文件夹不存在")
    return normalized


@router.get("/directory-priorities")
async def directory_priorities(
    scope: str = "",
    session_hash: str = Depends(require_session),
):
    try:
        return {
            "items": await media_directories.immediate_preferences(scope),
            "minimum": media_directories.MIN_PREFERENCE,
            "maximum": media_directories.MAX_PREFERENCE,
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/directory-priority")
async def directory_priority(
    payload: DirectoryPriorityChange,
    request: Request,
    session_hash: str = Depends(require_session),
):
    path = await _require_existing_directory(payload.path)
    try:
        result = await media_directories.set_preference(path, payload.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await admin_service.audit(
        session_hash,
        "directory_priority",
        1,
        path,
        "success",
        json.dumps({"preference": payload.value}, ensure_ascii=False),
        request,
    )
    return result


@router.post("/directory/rename")
async def rename_directory(
    payload: DirectoryRename,
    request: Request,
    session_hash: str = Depends(require_session),
):
    await _require_existing_directory(payload.path)
    try:
        result = await media_directories.rename_directory(payload.path, payload.new_name)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文件夹不存在") from exc
    except FileExistsError as exc:
        raise HTTPException(409, "目标文件夹名称已存在") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await admin_service.audit(
        session_hash,
        "directory_rename",
        int(result.get("media_count", 1)),
        payload.path,
        "success",
        json.dumps(result, ensure_ascii=False),
        request,
    )
    return result
