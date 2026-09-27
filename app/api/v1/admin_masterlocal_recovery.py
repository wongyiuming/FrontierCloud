"""Recover expired MasterLocal reservations before accepting new pool work."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, text

from app.api.v1 import admin_cluster_integrity as cluster
from app.api.v1 import admin_delete_integrity as delete_integrity
from app.services import resource_pool, storage_capacity
from app.services.federation import protocol as p
from app.services.federation import schema as s
from app.services.federation.state import state as node_state
from app.services.media_manager import MEDIA_ROOT


router = APIRouter()


async def recover_expired_masterlocal(limit: int = 50) -> int:
    """Remove bytes owned by expired local reservations before releasing quota."""
    now = int(time.time())
    async with node_state.database.connect() as conn:
        upload_ids = list((await conn.execute(
            select(s.upload_sessions.c.upload_id)
            .join(s.storage_members,
                  s.upload_sessions.c.storage_member_id == s.storage_members.c.member_id)
            .where(
                s.upload_sessions.c.state == "reserved",
                s.upload_sessions.c.expires_at <= now,
                s.storage_members.c.member_kind == "MasterLocal",
            )
            .order_by(s.upload_sessions.c.created_at)
            .limit(limit)
        )).scalars())

    recovered = 0
    for upload_id in upload_ids:
        try:
            row = await resource_pool.upload_session(str(upload_id), node_state.database)
        except p.ProtocolError:
            continue
        if (row["state"] != "reserved" or int(row["expires_at"]) > now
                or row["member_kind"] != "MasterLocal"):
            continue
        async with node_state.database.connect() as conn:
            committed = bool(await conn.scalar(select(s.global_media.c.media_id).where(
                s.global_media.c.media_id == row["media_id"]
            )))
        if committed:
            continue
        target = (MEDIA_ROOT / row["media_path"]).resolve()
        if MEDIA_ROOT not in target.parents:
            continue
        target.unlink(missing_ok=True)
        async with node_state.database.begin() as conn:
            await conn.execute(text("DELETE FROM media_objects WHERE media_id=:id"),
                               {"id": row["media_id"]})
        await resource_pool.fail_upload(str(upload_id), node_state.database)
        recovered += 1
    return recovered


@router.get("/storage-pool")
async def storage_pool(session_hash: str = Depends(cluster.require_session)):
    await recover_expired_masterlocal()
    summary = await cluster.storage_pool(session_hash)
    return await storage_capacity.enrich_pool_summary(summary, node_state)


@router.post("/upload/session")
async def create_upload_session(payload: cluster.ClusterUploadReservation, request: Request,
                                session_hash: str = Depends(cluster.require_session)):
    await recover_expired_masterlocal()
    path = await cluster._upload_logical_path(payload)
    await delete_integrity.reconcile_upload_path(path)
    return await cluster.create_upload_session(payload, request, session_hash)
