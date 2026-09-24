"""Crash-safe Follower storage publish path."""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text, update

from app.api import internal_nodes as legacy_internal
from app.services import resource_pool
from app.services.federation import schema as s
from app.services.federation.state import state
from app.services.media_manager import MEDIA_ROOT, MediaManager, SIGNATURES


router = APIRouter()


@router.put("/storage/{original}")
async def storage_upload(request: Request, original: str):
    relation, value = await legacy_internal.storage_capability(request, original, "upload")
    expected = int(value["size"])
    target = (MEDIA_ROOT / value["path"]).resolve()
    if MEDIA_ROOT not in target.parents or target.exists():
        raise HTTPException(409, "Storage target already exists or is invalid")
    if expected <= 0:
        raise HTTPException(413, "Upload size is invalid")

    reserved = False
    async with state.database.begin() as conn:
        member = (await conn.execute(select(s.storage_members).where(
            s.storage_members.c.member_id == state.node["node_id"]
        ).with_for_update())).mappings().first()
        physical_available = resource_pool.physical_free(MEDIA_ROOT) - resource_pool.PHYSICAL_RESERVE_BYTES
        logical_available = (int(member["allocated_bytes"]) - int(member["used_bytes"])
                             - int(member["reserved_bytes"])) if member else 0
        if (not member or not member["storage_enabled"] or not member["writable"]
                or min(physical_available, logical_available) < expected):
            raise HTTPException(507, "Follower storage is not writable or lacks capacity")
        await conn.execute(update(s.storage_members).where(
            s.storage_members.c.member_id == state.node["node_id"]
        ).values(reserved_bytes=s.storage_members.c.reserved_bytes + expected,
                 physical_free_bytes=resource_pool.physical_free(MEDIA_ROOT), updated_at=int(time.time())))
        reserved = True

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = (MEDIA_ROOT / f".cluster-upload-{original}.part").resolve()
    written, digest, head, published = 0, hashlib.sha256(), b"", False
    try:
        with temporary.open("xb") as output:
            async for chunk in request.stream():
                if not head:
                    head = chunk[:4096]
                written += len(chunk)
                if written > expected:
                    raise HTTPException(413, "Upload exceeds reserved size")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if written != expected:
            raise HTTPException(400, "Upload size does not match reservation")
        extension = target.suffix.lower()
        if not SIGNATURES.get(extension, lambda _data: False)(head):
            raise HTTPException(400, "媒体内容与扩展名不匹配")
        MediaManager._validate_media_destination(target)
        os.replace(temporary, target)
        published = True

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with state.database.begin() as conn:
            await conn.execute(text("""
                INSERT INTO media_objects
                (media_id, object_kind, media_path, path_locator, created_at, updated_at)
                VALUES (:media_id, :kind, :path, :locator, :now, :now)
            """), {
                "media_id": original,
                "kind": "audio" if value["path"].startswith("music/") else "video",
                "path": value["path"],
                "locator": hashlib.sha256(value["path"].encode()).hexdigest(),
                "now": now,
            })
            await conn.execute(update(s.storage_members).where(
                s.storage_members.c.member_id == state.node["node_id"]
            ).values(reserved_bytes=func.greatest(0, s.storage_members.c.reserved_bytes - expected),
                     used_bytes=s.storage_members.c.used_bytes + written,
                     physical_free_bytes=resource_pool.physical_free(MEDIA_ROOT), updated_at=int(time.time())))
        reserved = False
        published = False
        return JSONResponse(
            {"object_id": original, "size_bytes": written,
             "sha256": digest.hexdigest(), "etag": f'"{digest.hexdigest()}"'},
            headers=legacy_internal.storage_cors(relation, request.headers.get("origin")),
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        if published:
            target.unlink(missing_ok=True)
            try:
                async with state.database.begin() as conn:
                    await conn.execute(text("DELETE FROM media_objects WHERE media_id=:media_id"),
                                       {"media_id": original})
            except Exception:
                pass
        if reserved:
            async with state.database.begin() as conn:
                await conn.execute(update(s.storage_members).where(
                    s.storage_members.c.member_id == state.node["node_id"]
                ).values(reserved_bytes=func.greatest(
                    0, s.storage_members.c.reserved_bytes - expected), updated_at=int(time.time())))
        raise


def install() -> None:
    legacy_internal.router.routes[:] = [
        route for route in legacy_internal.router.routes
        if not (route.path == "/internal/v1/storage/{original}" and "PUT" in (route.methods or set()))
    ]
    legacy_internal.router.include_router(router)
