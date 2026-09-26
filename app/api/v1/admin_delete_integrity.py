"""Recoverable Master media deletion and upload-path reconciliation."""
from __future__ import annotations

import json
import logging
import ssl
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, text, update

from app.api.v1 import admin as legacy_admin
from app.api.v1 import admin_cluster_integrity as cluster
from app.services import admin_service, lyrics, resource_pool
from app.services.federation import protocol as p
from app.services.federation import schema as s
from app.services.federation.state import state as node_state
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import MEDIA_ROOT, MediaManager


router = APIRouter()
require_session = legacy_admin.require_session
logger = logging.getLogger("frontiercloud.admin")


async def _pending_media(media_id: str) -> dict | None:
    async with node_state.database.connect() as conn:
        row = (await conn.execute(select(s.global_media).where(
            s.global_media.c.media_id == media_id,
            s.global_media.c.state == "pending_delete",
        ))).mappings().first()
    return dict(row) if row else None


async def retry_pending_media(media_id: str) -> bool:
    """Converge one pending deletion; missing bytes are already a successful delete."""
    row = await _pending_media(media_id)
    if row is None:
        return True
    try:
        if row["storage_member_id"] == node_state.node["node_id"]:
            target = (MEDIA_ROOT / row["media_path"]).resolve()
            if MEDIA_ROOT not in target.parents:
                return False
            target.unlink(missing_ok=True)
        else:
            member = next((item for item in await resource_pool.list_members(node_state.database)
                           if item["member_id"] == row["storage_member_id"]), None)
            if not member or not member.get("relationship_id") or member.get("health") != "online":
                return False
            relation = await node_state.relationship(member["relationship_id"])
            if relation.get("state") != "active" or relation.get("status") == "offline":
                return False
            token = p.storage_token(
                node_state.unseal(relation["credential"]), relation["relationship_id"],
                node_state.node["node_id"], row["storage_member_id"], row["media_id"], row["object_id"],
                "delete", row["media_path"], int(row["size_bytes"]), int(time.time()),
            )
            async with httpx.AsyncClient(
                verify=ssl.create_default_context(), trust_env=False,
                timeout=httpx.Timeout(20, connect=8),
            ) as client:
                response = await client.post(
                    relation["peer_endpoint"] + f"/internal/v1/storage/{row['object_id']}/delete",
                    headers={"X-Storage-Capability": token},
                )
            if response.status_code != 200:
                return False

        now = int(time.time())
        async with node_state.database.begin() as conn:
            await conn.execute(text("DELETE FROM media_lyric_links WHERE media_id=:id"), {"id": row["media_id"]})
            await conn.execute(text("DELETE FROM media_playback_events WHERE media_id=:id"), {"id": row["media_id"]})
            await conn.execute(text("DELETE FROM media_playback_stats WHERE media_id=:id"), {"id": row["media_id"]})
            if row["storage_member_id"] == node_state.node["node_id"]:
                await conn.execute(text("DELETE FROM media_objects WHERE media_id=:id"), {"id": row["object_id"]})
            await conn.execute(text("""
                UPDATE cluster_storage_members
                SET used_bytes=CASE
                    WHEN used_bytes > :size THEN used_bytes - :size
                    ELSE 0
                END,
                updated_at=:now
                WHERE member_id=:member_id
            """), {"size": int(row["size_bytes"]), "now": now,
                     "member_id": row["storage_member_id"]})
            await conn.execute(text("DELETE FROM global_media_objects WHERE media_id=:id"),
                               {"id": row["media_id"]})
        return True
    except Exception as exc:
        logger.warning(
            "pending_media_delete_retry_failed media_id=%s path=%s member_id=%s error=%s",
            row["media_id"], row["media_path"], row["storage_member_id"], exc,
        )
        return False


async def reconcile_upload_path(path: str) -> None:
    """Remove safe stale owners of a path before creating a new reservation."""
    locator = resource_pool.path_locator(path)
    now = int(time.time())

    # Completed sessions must never retain the unique path lease. Older builds
    # could leave this transient locator populated and block re-uploads forever.
    async with node_state.database.begin() as conn:
        await conn.execute(update(s.upload_sessions).where(
            s.upload_sessions.c.path_locator == locator,
            s.upload_sessions.c.state == "complete",
        ).values(path_locator=None, updated_at=now))

    async with node_state.database.connect() as conn:
        media = (await conn.execute(select(s.global_media.c.media_id, s.global_media.c.state).where(
            s.global_media.c.path_locator == locator,
        ))).mappings().first()
    if media:
        if media["state"] != "pending_delete":
            raise HTTPException(409, "全局媒体路径已存在")
        if not await retry_pending_media(str(media["media_id"])):
            raise HTTPException(409, "同路径媒体仍在等待存储节点完成删除，请稍后重试")

    async with node_state.database.connect() as conn:
        upload = (await conn.execute(select(s.upload_sessions).where(
            s.upload_sessions.c.path_locator == locator,
        ).order_by(s.upload_sessions.c.created_at.desc()))).mappings().first()
    if not upload:
        return
    if upload["state"] == "reserved" and int(upload["expires_at"]) <= now:
        if await cluster._cleanup_upload_session(str(upload["upload_id"]), allow_unindexed_local=True):
            return
        raise HTTPException(409, "同路径旧上传已过期，但存储对象尚未完成清理，请稍后重试")
    if upload["state"] == "reserved":
        raise HTTPException(409, "同路径已有上传正在进行；失败任务会自动取消预留")
    raise HTTPException(409, "同路径存在未收敛的上传状态，请稍后重试")


@router.delete("/upload/session/{upload_id}")
async def cancel_upload_session(upload_id: str, request: Request,
                                session_hash: str = Depends(require_session)):
    cleaned = await cluster._cleanup_upload_session(upload_id, allow_unindexed_local=True)
    await admin_service.audit(
        session_hash, "upload-cancelled", 1, upload_id,
        "success" if cleaned else "pending", "", request,
    )
    if not cleaned:
        raise HTTPException(409, "上传取消已记录，但存储节点暂未完成清理")
    return {"status": "cancelled"}


@router.post("/delete")
async def delete_objects(request: Request, payload: dict,
                         session_hash: str = Depends(require_session)):
    paths = payload.get("paths")
    if (not isinstance(paths, list) or not paths
            or len(paths) > legacy_admin.settings.ADMIN_MAX_BATCH_FILES):
        raise HTTPException(400, "请选择合法对象")
    normalized = [MediaManager.normalize_relative(str(path)) for path in paths]
    if any(
        path == lyrics.DEFAULT_LYRIC_PATH
        or lyrics.DEFAULT_LYRIC_PATH.startswith(path.rstrip("/") + "/")
        for path in normalized
    ):
        raise HTTPException(409, "系统默认歌词为保留对象，不能删除")
    if node_state.node["role"] != "Master" or not all(
            str(path).split("/", 1)[0] in {"music", "vido"} for path in paths):
        return await legacy_admin.delete_objects(request, payload, session_hash)

    rows = await resource_pool.list_media(node_state.database)
    selected = [row for row in rows if any(
        row["media_path"] == path or row["media_path"].startswith(path.rstrip("/") + "/")
        for path in normalized
    )]
    if not selected:
        raise HTTPException(404, "对象不存在")

    await resource_pool.mark_pending_delete([row["media_id"] for row in selected], node_state.database)
    deleted = 0
    pending: list[str] = []
    for row in selected:
        if await retry_pending_media(row["media_id"]):
            deleted += 1
        else:
            pending.append(row["media_path"])

    await admin_service.audit(
        session_hash, "global-media-delete", len(selected),
        json.dumps(normalized, ensure_ascii=False),
        "success" if not pending else "pending",
        json.dumps({"deleted": deleted, "pending_delete": pending}, ensure_ascii=False), request,
    )
    await invalidate_media_catalog()
    return {"deleted": deleted, "pending_delete": pending}
