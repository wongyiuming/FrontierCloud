"""Master-only Storage Pool integrity routes.

This module closes the gap between the legacy single-node Admin filesystem API
and the Master-owned global storage catalog. Master media mutations must never
silently fall back to the local filesystem.
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from zipstream import ZIP_STORED, ZipStream

from app.api.v1 import admin as legacy_admin
from app.services import admin_service, resource_pool
from app.services.federation import protocol as p
from app.services.federation import schema as s
from app.services.federation.catalog import catalog as node_catalog
from app.services.federation.runtime import runtime as node_runtime
from app.services.federation.state import state as node_state
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import MEDIA_ROOT, MediaManager, SIGNATURES


router = APIRouter()
require_session = legacy_admin.require_session
_mutation_audit = legacy_admin._mutation_audit


class ClusterUploadReservation(BaseModel):
    storage_member_id: str | None = Field(None, max_length=64)
    target_dir: str = Field(max_length=1024)
    relative_path: str | None = Field(None, max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=10 * 1024 ** 3)


def _preferred_member(value: str | None) -> str | None:
    value = str(value or "").strip().lower()
    if value in {"", "auto"}:
        return None
    if not p.IDENTIFIER.fullmatch(value):
        raise HTTPException(400, "存储节点无效")
    return value


async def _upload_logical_path(payload: ClusterUploadReservation) -> str:
    """Build the final logical path first, then validate its final layout."""
    try:
        target = MediaManager.normalize_relative(payload.target_dir) if payload.target_dir else ""
        source = payload.relative_path or payload.filename
        relative = MediaManager.normalize_relative(source)
        parts = relative.split("/")
        parts[-1] = MediaManager.validate_name(parts[-1])
        logical = "/".join(([target] if target else []) + parts)
        logical = resource_pool.validate_media_path(logical)[0]
    except HTTPException:
        raise
    except p.ProtocolError as exc:
        raise HTTPException(400, str(exc)) from exc

    category = "/".join(logical.split("/")[:2])
    existing = await node_catalog.resources(root=category)
    depth = len(logical.split("/"))
    if any(len(str(row["path"]).split("/")) != depth for row in existing):
        detail = ("该分类已使用子目录，禁止在分类目录直接上传媒体" if depth == 3
                  else "该分类已有直接媒体，禁止再使用子目录存放媒体")
        raise HTTPException(409, detail)
    return logical


async def _reserve_upload(path: str, size: int, preferred: str | None) -> dict:
    """Reserve without discarding expired rows whose physical state is unknown."""
    path, kind = resource_pool.validate_media_path(path)
    member = await resource_pool.choose_member(size, preferred, node_state.database)
    now, upload_id = int(time.time()), uuid.uuid4().hex
    media_id = hashlib.sha256((upload_id + ":" + path).encode()).hexdigest()
    try:
        async with node_state.database.begin() as conn:
            current = (await conn.execute(select(s.storage_members).where(
                s.storage_members.c.member_id == member["member_id"]).with_for_update()
            )).mappings().first()
            logical_available = (int(current["allocated_bytes"]) - int(current["used_bytes"])
                                 - int(current["reserved_bytes"])) if current else 0
            physical_available = max(0, int(current["physical_free_bytes"])
                                     - resource_pool.PHYSICAL_RESERVE_BYTES) if current else 0
            if current and current["member_kind"] == "MasterLocal":
                physical_available = max(
                    0, resource_pool.physical_free(MEDIA_ROOT) - resource_pool.PHYSICAL_RESERVE_BYTES,
                )
            if (not current or not current["storage_enabled"] or current["health"] != "online"
                    or not current["writable"] or min(logical_available, physical_available) < size):
                raise p.ProtocolError("存储成员状态或剩余配额已变化，请刷新后重试")
            locator = resource_pool.path_locator(path)
            if await conn.scalar(select(s.global_media.c.media_id).where(
                    s.global_media.c.path_locator == locator)):
                raise p.ProtocolError("全局媒体路径已存在")
            await conn.execute(update(s.storage_members).where(
                s.storage_members.c.member_id == member["member_id"]
            ).values(reserved_bytes=s.storage_members.c.reserved_bytes + size, updated_at=now))
            await conn.execute(insert(s.upload_sessions).values(
                upload_id=upload_id, storage_member_id=member["member_id"], media_id=media_id,
                media_path=path, path_locator=locator, object_kind=kind, expected_bytes=size,
                state="reserved", expires_at=now + resource_pool.UPLOAD_TTL_SECONDS,
                created_at=now, updated_at=now,
            ))
    except IntegrityError as exc:
        raise p.ProtocolError("全局媒体路径已存在或已有上传正在进行") from exc
    return {"upload_id": upload_id, "media_id": media_id, "path": path,
            "type": kind, "member": member}


async def _local_object_exists(row: dict) -> bool:
    async with node_state.database.connect() as conn:
        return bool(await conn.scalar(text("""
            SELECT COUNT(*) FROM media_objects
            WHERE media_id=:media_id AND BINARY media_path=BINARY :media_path
        """), {"media_id": row["media_id"], "media_path": row["media_path"]}))


async def _member_relation(member_id: str):
    member = next((item for item in await resource_pool.list_members(node_state.database)
                   if item["member_id"] == member_id), None)
    if not member or not member.get("relationship_id"):
        return member, None
    relation = await node_state.relationship(member["relationship_id"])
    return member, relation


async def _cleanup_upload_session(upload_id: str, *, allow_unindexed_local: bool = False) -> bool:
    """Release a reservation only after its possible physical object is gone."""
    try:
        row = await resource_pool.upload_session(upload_id, node_state.database)
    except p.ProtocolError:
        return True
    if row["state"] == "complete":
        return True
    if row["state"] != "reserved":
        return False

    if row["member_kind"] == "MasterLocal":
        target = (MEDIA_ROOT / row["media_path"]).resolve()
        if MEDIA_ROOT not in target.parents:
            return False
        exists = target.is_file()
        indexed = await _local_object_exists(row) if exists else False
        if exists and not (indexed or allow_unindexed_local):
            return False
        if exists:
            target.unlink(missing_ok=True)
        async with node_state.database.begin() as conn:
            await conn.execute(text("DELETE FROM media_objects WHERE media_id=:id"), {"id": row["media_id"]})
        await resource_pool.fail_upload(upload_id, node_state.database)
        return True

    member, relation = await _member_relation(row["storage_member_id"])
    if not member or not relation or relation.get("state") != "active" or relation.get("status") == "offline":
        return False
    try:
        await node_runtime.call(
            relation,
            f"/internal/v1/storage/{row['media_id']}/stat",
            {"path": row["media_path"]},
        )
    except p.ProtocolError as exc:
        if "HTTP 404" not in str(exc):
            return False
        await resource_pool.fail_upload(upload_id, node_state.database)
        return True

    token = p.storage_token(
        node_state.unseal(relation["credential"]), relation["relationship_id"],
        node_state.node["node_id"], row["storage_member_id"], row["media_id"], row["media_id"],
        "delete", row["media_path"], int(row["expected_bytes"]), int(time.time()),
    )
    try:
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(), trust_env=False, timeout=httpx.Timeout(20, connect=8),
        ) as client:
            response = await client.post(
                relation["peer_endpoint"] + f"/internal/v1/storage/{row['media_id']}/delete",
                headers={"X-Storage-Capability": token},
            )
        if response.status_code != 200:
            return False
    except httpx.HTTPError:
        return False
    await resource_pool.fail_upload(upload_id, node_state.database)
    return True


async def _cleanup_expired_uploads(limit: int = 50) -> int:
    now = int(time.time())
    async with node_state.database.connect() as conn:
        upload_ids = list((await conn.execute(select(s.upload_sessions.c.upload_id).where(
            s.upload_sessions.c.state == "reserved", s.upload_sessions.c.expires_at <= now,
        ).order_by(s.upload_sessions.c.created_at).limit(limit))).scalars())
    cleaned = 0
    for upload_id in upload_ids:
        cleaned += int(await _cleanup_upload_session(str(upload_id)))
    return cleaned


async def _hidden_paths() -> set[str]:
    async with node_state.database.connect() as conn:
        result = await conn.execute(text("SELECT relative_path FROM media_visibility WHERE hidden=1"))
        return {str(row[0]) for row in result.fetchall()}


def _path_hidden(path: str, hidden: set[str]) -> bool:
    parts = path.split("/")
    return any("/".join(parts[:index]) in hidden for index in range(1, len(parts) + 1))


async def _global_tree(path: str, query: str | None = None) -> dict:
    scope = legacy_admin._media_priority_scope(path)
    parts = scope.split("/")
    if len(parts) > 3:
        raise HTTPException(400, "目录不在受支持的媒体层级内")
    rows = await node_catalog.resources(directory=scope)
    hidden = await _hidden_paths()
    if query:
        normalized = legacy_admin.media_search.normalized_query(query)
        matches = []
        for row in rows:
            if legacy_admin.media_search.matches_search(
                legacy_admin.media_search.build_search_text(row["path"].rsplit("/", 1)[-1], row["path"]),
                normalized,
            ):
                matches.append({
                    "name": row["path"].rsplit("/", 1)[-1], "path": row["path"], "kind": "file",
                    "size": row["payload"]["size"], "hidden": _path_hidden(row["path"], hidden),
                    "media": True, "hideable": False, "media_id": row["resource_id"],
                    "storage_member_id": row["owner_id"], "transport": row.get("transport"),
                    "node_health": row.get("health"),
                })
        return {"path": path, "query": query, "items": matches[:legacy_admin.media_search.MAX_SEARCH_RESULTS],
                "truncated": len(matches) > legacy_admin.media_search.MAX_SEARCH_RESULTS}

    prefix = scope.rstrip("/") + "/"
    items: dict[str, dict] = {}
    for row in rows:
        remainder = row["path"][len(prefix):]
        if "/" in remainder:
            name = remainder.split("/", 1)[0]
            child = prefix + name
            items.setdefault(child, {
                "name": name, "path": child, "kind": "directory", "size": None,
                "hidden": _path_hidden(child, hidden), "media": False, "hideable": True,
            })
        else:
            items[row["path"]] = {
                "name": remainder, "path": row["path"], "kind": "file",
                "size": row["payload"]["size"], "hidden": _path_hidden(row["path"], hidden),
                "media": True, "hideable": False, "media_id": row["resource_id"],
                "storage_member_id": row["owner_id"], "transport": row.get("transport"),
                "node_health": row.get("health"),
            }
    return {"path": scope,
            "items": sorted(items.values(), key=lambda item: (item["kind"] != "directory", item["name"].casefold()))}


@router.get("/tree")
async def admin_tree(request: Request, path: str = "", session_hash: str = Depends(require_session)):
    if node_state.node["role"] == "Master" and path and path.split("/", 1)[0] in {"music", "vido"}:
        return await _global_tree(path)
    return await legacy_admin.admin_tree(request, path, session_hash)


@router.get("/tree/search")
async def admin_tree_search(request: Request, q: str, path: str = "",
                            session_hash: str = Depends(require_session)):
    if node_state.node["role"] == "Master" and path.split("/", 1)[0] in {"music", "vido"}:
        return await _global_tree(path, q)
    return await legacy_admin.admin_tree_search(request, q, path, session_hash)


@router.get("/storage-pool")
async def storage_pool(session_hash: str = Depends(require_session)):
    if node_state.node["role"] != "Master":
        return {"members": [], "standalone": True}
    await _cleanup_expired_uploads()
    summary = await resource_pool.pool_summary(node_state.database)
    writable = [member for member in summary["members"]
                if member["storage_enabled"] and member["health"] == "online" and member["writable"]]
    auto_available = max((int(member["available_bytes"]) for member in writable), default=0)
    auto = {
        "member_id": "auto", "member_kind": "Auto", "transport": "Automatic",
        "storage_enabled": 1, "allocated_bytes": 0, "used_bytes": 0, "reserved_bytes": 0,
        "physical_free_bytes": 0, "health": "online", "writable": 1,
        "available_bytes": auto_available, "online_writable_bytes": auto_available,
        "offline_stored_bytes": 0, "compute": {}, "backup": {},
    }
    return {**summary, "members": [auto, *summary["members"]]}


@router.post("/upload/session")
async def create_upload_session(payload: ClusterUploadReservation, request: Request,
                                session_hash: str = Depends(require_session)):
    if node_state.node["role"] != "Master":
        raise HTTPException(409, "Storage Pool uploads require a Master")
    if payload.size_bytes > legacy_admin.settings.ADMIN_MAX_UPLOAD_FILE_SIZE:
        raise HTTPException(413, "文件超过单文件上传限制")
    await _cleanup_expired_uploads()
    path = await _upload_logical_path(payload)
    try:
        reservation = await _reserve_upload(path, payload.size_bytes, _preferred_member(payload.storage_member_id))
        member = reservation["member"]
        result = {
            "upload_id": reservation["upload_id"], "media_id": reservation["media_id"],
            "path": path, "transport": member["transport"], "member_id": member["member_id"],
            "upload_url": f"/api/v1/media/admin/upload/session/{reservation['upload_id']}/bytes",
        }
        if member["transport"] == "Direct":
            relation = await node_state.relationship(member["relationship_id"])
            token = p.storage_token(
                node_state.unseal(relation["credential"]), relation["relationship_id"],
                node_state.node["node_id"], member["member_id"], reservation["media_id"],
                reservation["media_id"], "upload", path, payload.size_bytes, int(time.time()),
            )
            result["upload_url"] = relation["peer_endpoint"] + f"/internal/v1/storage/{reservation['media_id']}?token={token}"
        await admin_service.audit(session_hash, "upload-reserved", 1, path, "success",
                                  member["member_id"], request)
        return result
    except p.ProtocolError as exc:
        raise HTTPException(409, str(exc)) from exc


async def _receive_master_local(upload_id: str, request: Request, row: dict) -> dict:
    destination = (MEDIA_ROOT / row["media_path"]).resolve()
    if MEDIA_ROOT not in destination.parents or destination.exists():
        raise HTTPException(409, "目标位置已存在同名文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = (MEDIA_ROOT / f".cluster-upload-{upload_id}.part").resolve()
    written, digest, head, published = 0, hashlib.sha256(), b"", False
    try:
        with temporary.open("xb") as output:
            async for chunk in request.stream():
                if not head:
                    head = chunk[:4096]
                written += len(chunk)
                if written > int(row["expected_bytes"]):
                    raise HTTPException(413, "上传内容超过预留大小")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if written != int(row["expected_bytes"]):
            raise HTTPException(400, "上传内容大小与预留不一致")
        if not SIGNATURES.get(destination.suffix.lower(), lambda _data: False)(head):
            raise HTTPException(400, "媒体内容与扩展名不匹配")
        os.replace(temporary, destination)
        published = True
        now_epoch = int(time.time())
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        etag = f'"{digest.hexdigest()}"'
        async with node_state.database.begin() as conn:
            locked = (await conn.execute(select(s.upload_sessions).where(
                s.upload_sessions.c.upload_id == upload_id).with_for_update()
            )).mappings().first()
            if not locked or locked["state"] != "reserved" or locked["expires_at"] <= now_epoch:
                raise p.ProtocolError("Upload reservation is missing or expired")
            await conn.execute(text("""
                INSERT INTO media_objects
                (media_id, object_kind, media_path, path_locator, created_at, updated_at)
                VALUES (:media_id, :kind, :path, :locator, :now, :now)
            """), {"media_id": row["media_id"], "kind": row["object_kind"], "path": row["media_path"],
                     "locator": resource_pool.path_locator(row["media_path"]), "now": now_dt})
            await conn.execute(insert(s.global_media).values(
                media_id=row["media_id"], storage_member_id=row["storage_member_id"],
                object_id=row["media_id"], media_path=row["media_path"],
                path_locator=resource_pool.path_locator(row["media_path"]), object_kind=row["object_kind"],
                size_bytes=written, etag=etag, state="active", created_at=now_epoch, updated_at=now_epoch,
            ))
            await conn.execute(update(s.storage_members).where(
                s.storage_members.c.member_id == row["storage_member_id"]
            ).values(reserved_bytes=func.greatest(0, s.storage_members.c.reserved_bytes - row["expected_bytes"]),
                     used_bytes=s.storage_members.c.used_bytes + written, updated_at=now_epoch))
            await conn.execute(update(s.upload_sessions).where(
                s.upload_sessions.c.upload_id == upload_id
            ).values(state="complete", path_locator=None, updated_at=now_epoch))
        published = False
        await invalidate_media_catalog()
        return {"path": row["media_path"], "media_id": row["media_id"]}
    except BaseException:
        temporary.unlink(missing_ok=True)
        if published:
            destination.unlink(missing_ok=True)
        raise


@router.put("/upload/session/{upload_id}/bytes")
async def upload_session_bytes(upload_id: str, request: Request,
                               session_hash: str = Depends(require_session)):
    allow_unindexed_local = False
    try:
        row = await resource_pool.upload_session(upload_id, node_state.database)
        if row["state"] != "reserved" or row["expires_at"] <= int(time.time()):
            raise p.ProtocolError("Upload reservation is unavailable")
        if row["member_kind"] == "MasterLocal":
            allow_unindexed_local = True
            result = await _receive_master_local(upload_id, request, row)
            await admin_service.audit(session_hash, "upload-finalized", 1, row["media_path"], "success",
                                      row["storage_member_id"], request)
            return result
        relation = await node_state.relationship(row["relationship_id"])
        token = p.storage_token(
            node_state.unseal(relation["credential"]), relation["relationship_id"],
            node_state.node["node_id"], row["storage_member_id"], row["media_id"], row["media_id"],
            "upload", row["media_path"], int(row["expected_bytes"]), int(time.time()),
        )
        headers = {"X-Storage-Capability": token, "Content-Type": "application/octet-stream"}
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(), trust_env=False, timeout=httpx.Timeout(None, connect=10),
        ) as client:
            async with client.stream(
                "PUT", relation["peer_endpoint"] + f"/internal/v1/storage/{row['media_id']}",
                headers=headers, content=request.stream(),
            ) as upstream:
                payload = await upstream.aread()
                if upstream.status_code != 200 or len(payload) > p.MAX_CONTROL_BYTES:
                    raise p.ProtocolError(f"Follower storage upload HTTP {upstream.status_code}")
                result = json.loads(payload)
        media = await resource_pool.finalize_upload(
            upload_id, object_id=result["object_id"], actual_size=int(result["size_bytes"]),
            etag=str(result["etag"]), database=node_state.database,
        )
        await invalidate_media_catalog()
        await admin_service.audit(session_hash, "upload-finalized", 1, row["media_path"], "success",
                                  row["storage_member_id"], request)
        return {"path": media["media_path"], "media_id": media["media_id"]}
    except Exception as exc:
        cleaned = await _cleanup_upload_session(upload_id, allow_unindexed_local=allow_unindexed_local)
        if not cleaned:
            await admin_service.audit(session_hash, "upload-cleanup-deferred", 1, upload_id, "pending", "", request)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(409, str(exc) if isinstance(exc, p.ProtocolError) else "存储上传失败") from exc


@router.post("/upload/session/{upload_id}/finalize")
async def finalize_direct_upload(upload_id: str, request: Request,
                                 session_hash: str = Depends(require_session)):
    try:
        row = await resource_pool.upload_session(upload_id, node_state.database)
        if row["transport"] != "Direct" or not row["relationship_id"]:
            raise p.ProtocolError("Upload session is not Direct")
        relation = await node_state.relationship(row["relationship_id"])
        result = await node_runtime.call(
            relation, f"/internal/v1/storage/{row['media_id']}/stat", {"path": row["media_path"]},
        )
        media = await resource_pool.finalize_upload(
            upload_id, object_id=result["object_id"], actual_size=int(result["size_bytes"]),
            etag=str(result["etag"]), database=node_state.database,
        )
        await invalidate_media_catalog()
        await admin_service.audit(session_hash, "upload-finalized", 1, row["media_path"], "success",
                                  row["storage_member_id"], request)
        return {"path": media["media_path"], "media_id": media["media_id"]}
    except Exception as exc:
        cleaned = await _cleanup_upload_session(upload_id)
        if not cleaned:
            await admin_service.audit(session_hash, "upload-cleanup-deferred", 1, upload_id, "pending", "", request)
        raise HTTPException(409, str(exc) if isinstance(exc, p.ProtocolError) else "Direct 上传校验失败") from exc


@router.post("/upload/item")
async def upload_item(request: Request, file: UploadFile, target_dir: str = "", relative_path: str | None = None,
                      session_hash: str = Depends(require_session)):
    if node_state.node["role"] == "Master":
        source = relative_path or file.filename or ""
        try:
            await admin_service.audit(session_hash, "upload_item", 1, source, "failed",
                                      "Master requires Storage Pool upload", request)
        finally:
            await file.close()
        raise HTTPException(409, "Master 媒体上传必须通过 Storage Pool；请刷新管理页后重试")
    return await legacy_admin.upload_item(request, file, target_dir, relative_path, session_hash)


@router.post("/hide")
async def hide_objects(request: Request, payload: dict, session_hash: str = Depends(require_session)):
    paths, hidden = payload.get("paths"), payload.get("hidden", True)
    if (not isinstance(paths, list) or not paths or len(paths) > legacy_admin.settings.ADMIN_MAX_BATCH_FILES
            or not isinstance(hidden, bool)):
        raise HTTPException(400, "隐藏参数无效")
    if node_state.node["role"] != "Master" or not all(
            str(path).split("/", 1)[0] in {"music", "vido"} for path in paths):
        return await legacy_admin.hide_objects(request, payload, session_hash)
    normalized = []
    for raw in paths:
        rel = MediaManager.normalize_relative(str(raw))
        if len(rel.split("/")) > 3 or not await node_catalog.resources(directory=rel):
            raise HTTPException(404, f"全局媒体目录不存在: {rel}")
        normalized.append(rel)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with node_state.database.begin() as conn:
        for rel in normalized:
            if hidden:
                await conn.execute(text("""
                    INSERT INTO media_visibility(relative_path,hidden,updated_at)
                    VALUES(:path,1,:now)
                    ON DUPLICATE KEY UPDATE hidden=1,updated_at=:now
                """), {"path": rel, "now": now})
            else:
                await conn.execute(
                    text("DELETE FROM media_visibility WHERE "
                         + MediaManager._path_scope("relative_path", True)),
                    MediaManager._path_params(rel),
                )
        await _mutation_audit(session_hash, "hide" if hidden else "unhide", normalized, request)(
            conn, "success", len(normalized), {},
        )
    await invalidate_media_catalog()
    return {"status": "ok", "hidden": hidden}


async def _global_selection(paths: list[str]) -> tuple[list[dict], bool]:
    normalized = [MediaManager.normalize_relative(str(path)) for path in paths]
    rows = await node_catalog.resources()
    selected = []
    seen = set()
    exact_single = len(normalized) == 1 and any(row["path"] == normalized[0] for row in rows)
    for row in rows:
        if any(row["path"] == path or row["path"].startswith(path.rstrip("/") + "/") for path in normalized):
            if row["resource_id"] not in seen:
                selected.append(row)
                seen.add(row["resource_id"])
    if not selected:
        raise HTTPException(404, "对象不存在")
    return selected, exact_single


def _remote_source(row: dict, relation: dict):
    token = p.media_token(
        node_state.unseal(relation["credential"]), relation["relationship_id"],
        node_state.node["node_id"], row["owner_id"], row["object_id"], row["resource_id"], int(time.time()),
    )
    url = relation["peer_endpoint"] + f"/internal/v1/media/{row['object_id']}?token={token}"
    with httpx.Client(verify=ssl.create_default_context(), trust_env=False,
                      timeout=httpx.Timeout(None, connect=8), follow_redirects=False) as client:
        with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                raise RuntimeError(f"Follower media download HTTP {response.status_code}")
            for chunk in response.iter_bytes(1024 * 1024):
                yield chunk


async def _download_rows(rows: list[dict], exact_single: bool, request: Request, session_hash: str):
    relations: dict[str, dict] = {}
    for row in rows:
        if row["relationship_id"]:
            relation = await node_state.relationship(row["relationship_id"])
            if relation["state"] != "active" or relation["status"] == "offline":
                raise HTTPException(503, "所选媒体包含离线存储节点")
            relations[row["resource_id"]] = relation

    if exact_single and len(rows) == 1:
        row = rows[0]
        await admin_service.audit(session_hash, "download", 1, row["path"], "success", "global_single", request)
        if not row["relationship_id"]:
            path = (MEDIA_ROOT / row["path"]).resolve()
            if MEDIA_ROOT not in path.parents or not path.is_file():
                raise HTTPException(404, "本地媒体文件不存在")
            MediaManager.ensure_download_readable(path)
            return Response(headers={
                "X-Accel-Redirect": f"/_protected_media/{quote(row['path'], safe='/')}",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name, safe='')}",
                "Cache-Control": "private, no-store",
            })
        relation = relations[row["resource_id"]]
        return StreamingResponse(
            _remote_source(row, relation), media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row['path'].rsplit('/', 1)[-1], safe='')}",
                "Cache-Control": "private, no-store", "X-Accel-Buffering": "no",
            },
        )

    archive = ZipStream(compress_type=ZIP_STORED)
    for row in rows:
        if row["relationship_id"]:
            archive.add(_remote_source(row, relations[row["resource_id"]]), arcname=row["path"])
        else:
            path = (MEDIA_ROOT / row["path"]).resolve()
            if MEDIA_ROOT not in path.parents or not path.is_file():
                raise HTTPException(404, f"本地媒体文件不存在: {row['path']}")
            archive.add_path(path, arcname=row["path"])
    await admin_service.audit(session_hash, "download", len(rows), "global-selection", "success", "zip", request)
    return StreamingResponse(
        archive, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="media-download.zip"',
                 "Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/download")
async def download_objects(request: Request, paths: str, session_hash: str = Depends(require_session)):
    try:
        items = json.loads(paths)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "下载参数无效") from exc
    if not isinstance(items, list) or not items or len(items) > legacy_admin.settings.ADMIN_MAX_DOWNLOAD_ITEMS:
        raise HTTPException(400, "请选择合法下载对象")
    if node_state.node["role"] != "Master" or not all(
            str(path).split("/", 1)[0] in {"music", "vido"} for path in items):
        return await legacy_admin.download_objects(request, paths, session_hash)
    rows, exact_single = await _global_selection(items)
    return await _download_rows(rows, exact_single, request, session_hash)


@router.post("/nodes/{identifier}/revoke")
async def revoke_node(request: Request, identifier: str, actor: str = Depends(require_session)):
    from app.api.internal_nodes import require_https
    require_https(request)
    try:
        relation = await node_state.relationship(identifier)
        if node_state.node["role"] == "Master" and relation["direction"] == "downstream":
            async with node_state.database.connect() as conn:
                inflight = int(await conn.scalar(select(func.count()).select_from(s.upload_sessions).where(
                    s.upload_sessions.c.storage_member_id == relation["peer_id"],
                    s.upload_sessions.c.state == "reserved",
                )) or 0)
            if inflight:
                raise p.ProtocolError(f"Follower 仍有 {inflight} 个在途上传，完成或清理后才能撤销关系")
        elif node_state.node["role"] == "Follower" and relation["direction"] == "upstream":
            async with node_state.database.connect() as conn:
                reserved = int(await conn.scalar(select(s.storage_members.c.reserved_bytes).where(
                    s.storage_members.c.member_id == node_state.node["node_id"]
                )) or 0)
            if reserved:
                raise p.ProtocolError("Follower 仍有在途 Storage Pool 写入，完成后才能撤销关系")
        await node_runtime.revoke(identifier, actor)
        await invalidate_media_catalog()
        return {"state": "revoked"}
    except Exception as exc:
        raise HTTPException(409, str(exc) if isinstance(exc, p.ProtocolError)
                            else "节点关系撤销失败") from exc
