"""Serialize Master path mutations and route uploads by site type.

A rename is one exclusive path transaction. Other Master operations establish a
short shared fence while they validate and publish durable state. Media uploads
select a site type, never a concrete storage member; placement inside that type
is automatic for an empty media folder, then pinned to that folder's existing
storage member so every direct child media object stays colocated.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.v1 import admin as legacy_admin
from app.api.v1 import admin_cluster_integrity as cluster
from app.api.v1 import admin_delete_integrity as deletion
from app.api.v1 import admin_masterlocal_recovery as masterlocal
from app.services import media_directories, resource_pool, upload_site_routing
from app.services.federation import protocol as p
from app.services.federation.state import state as node_state
from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock


router = APIRouter()
require_session = legacy_admin.require_session


class SiteTypeUploadReservation(BaseModel):
    site_type: Literal["primary", "direct", "relay"]
    target_dir: str = Field(max_length=1024)
    relative_path: str | None = Field(None, max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=10 * 1024 ** 3)


def _is_master_media_paths(payload: dict) -> bool:
    paths = payload.get("paths") if isinstance(payload, dict) else None
    return bool(
        node_state.node.get("role") == "Master"
        and isinstance(paths, list)
        and paths
        and all(str(path).split("/", 1)[0] in {"music", "vido"} for path in paths)
    )


@router.post("/upload/session")
async def create_upload_session(
    payload: SiteTypeUploadReservation,
    request: Request,
    session_hash: str = Depends(require_session),
):
    if node_state.node.get("role") != "Master":
        raise HTTPException(409, "站点类型上传需要 Master")
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        # Multiple Admin sessions may reserve concurrently. Keep logical-path
        # validation, folder-affinity selection, and durable reservation in one
        # short lock so the first live reservation pins an empty media folder
        # before another chooser can place a sibling file elsewhere.
        async with resource_pool.storage_write_lock:
            base_payload = cluster.ClusterUploadReservation(
                storage_member_id=None,
                target_dir=payload.target_dir,
                relative_path=payload.relative_path,
                filename=payload.filename,
                size_bytes=payload.size_bytes,
            )
            logical_path = await cluster._upload_logical_path(base_payload)
            folder_path = upload_site_routing.media_folder_path(logical_path)
            try:
                member = await upload_site_routing.choose_member(
                    payload.site_type,
                    payload.size_bytes,
                    node_state.database,
                    folder_path=folder_path,
                )
            except p.ProtocolError as exc:
                raise HTTPException(409, str(exc)) from exc

            legacy_payload = base_payload.model_copy(update={
                "storage_member_id": str(member["member_id"]),
            })
            # Preserve the existing MasterLocal recovery/reconciliation layer.
            result = await masterlocal.create_upload_session(legacy_payload, request, session_hash)
        result["site_type"] = payload.site_type
        result["site_label"] = upload_site_routing.SITE_LABELS[payload.site_type]
        return result


@router.post("/hide")
async def hide_objects(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    if not _is_master_media_paths(payload):
        return await cluster.hide_objects(request, payload, session_hash)
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        return await cluster.hide_objects(request, payload, session_hash)


@router.post("/delete")
async def delete_objects(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    if not _is_master_media_paths(payload):
        return await deletion.delete_objects(request, payload, session_hash)
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        return await deletion.delete_objects(request, payload, session_hash)


async def _rename_with_master_lock(original, path: str, new_name: str):
    if node_state.node.get("role") != "Master":
        return await original(path, new_name)
    async with media_mutation_lock:
        ensure_media_mutations_ready()
        return await original(path, new_name)


def install() -> None:
    if getattr(media_directories, "_master_mutation_lock_installed", False):
        return
    original = media_directories.rename_directory

    async def rename_directory(path: str, new_name: str):
        return await _rename_with_master_lock(original, path, new_name)

    media_directories.rename_directory = rename_directory
    media_directories._master_mutation_lock_installed = True
