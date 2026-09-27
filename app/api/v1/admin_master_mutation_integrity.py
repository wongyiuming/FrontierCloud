"""Serialize Master path mutations against cross-member folder rename.

A rename is one exclusive path transaction. Other Master operations establish a
short shared fence while they validate and publish durable state (for example an
upload reservation or pending-delete marker). Once that durable state exists,
the rename preflight can see it and reject safely instead of racing it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.v1 import admin as legacy_admin
from app.api.v1 import admin_cluster_integrity as cluster
from app.api.v1 import admin_delete_integrity as deletion
from app.api.v1 import admin_masterlocal_recovery as masterlocal
from app.services import media_directories
from app.services.federation.state import state as node_state
from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock


router = APIRouter()
require_session = legacy_admin.require_session


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
    payload: cluster.ClusterUploadReservation,
    request: Request,
    session_hash: str = Depends(require_session),
):
    if node_state.node.get("role") != "Master":
        return await masterlocal.create_upload_session(payload, request, session_hash)
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        # Preserve the existing recovery/reconciliation layer. The fence wraps
        # that full reservation path; it must not bypass directly to cluster.
        return await masterlocal.create_upload_session(payload, request, session_hash)


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
