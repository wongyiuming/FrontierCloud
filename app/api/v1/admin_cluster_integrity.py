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
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from zipstream import ZIP_STORED, ZipStream

from app.api.v1 import admin as legacy_admin
from app.services import admin_service, resource_pool, media_crypto
from app.services.federation import protocol as p
from app.services.federation import schema as s
from app.services.federation.catalog import catalog as node_catalog
from app.services.federation.runtime import runtime as node_runtime
from app.services.federation.state import state as node_state
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import MEDIA_ROOT, MediaManager, SIGNATURES


router = APIRouter()
logger = logging.getLogger("frontiercloud.admin.upload")
require_session = legacy_admin.require_session
_mutation_audit = legacy_admin._mutation_audit


class ClusterUploadReservation(BaseModel):
    storage_member_id: str | None = Field(None, max_length=64)
    target_dir: str = Field(max_length=1024)
    relative_path: str | None = Field(None, max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=10 * 1024 ** 3)
    storage_mode: str = Field(pattern="^(encrypted|plain)$")
    encryption: dict | None = None
    preparation_token: str = Field("", max_length=4096)



async def _category_layout_conflict(category: str, depth: int) -> bool:
    """Check only for one incompatible path instead of hydrating a catalog.

    The old upload preflight loaded every object in the category and also
    joined playback/lyric metadata. Repeating that scan for every file made the
    pause between batch items grow with the category size.
    """
    path = s.global_media.c.media_path
    path_depth = func.length(path) - func.length(func.replace(path, "/", "")) + 1
    async with node_state.database.connect() as conn:
        conflict = await conn.scalar(
            select(s.global_media.c.media_id).where(
                s.global_media.c.state == "active",
                path.startswith(category.rstrip("/") + "/", autoescape=True),
                path_depth != depth,
            ).limit(1)
        )
    return conflict is not None


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
    depth = len(logical.split("/"))
    if await _category_layout_conflict(category, depth):
        detail = ("该分类已使用子目录，禁止在分类目录直接上传媒体" if depth == 3
                  else "该分类已有直接媒体，禁止再使用子目录存放媒体")
        raise HTTPException(409, detail)
    return logical


async def _reserve_upload(path: str, size: int, preferred: str | None, encryption=None) -> dict:
    """Reserve without discarding expired rows whose physical state is unknown."""
    path, kind = resource_pool.validate_media_path(path)
    member = await resource_pool.choose_member(size, preferred, node_state.database)
    now, upload_id = int(time.time()), uuid.uuid4().hex
    media_id = hashlib.sha256((upload_id + ":" + path).encode()).hexdigest()
    try:
        async with node_state.database.begin() as conn:
            from app.services import resource_fences
            await resource_fences.new_upload(conn,member['member_id'],'media',path)
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
                content_encrypted=int(encryption is not None),
                state="reserved", expires_at=now + resource_pool.UPLOAD_TTL_SECONDS,
                created_at=now, updated_at=now,
            ))
            from app.services import media_crypto
            await media_crypto.put_encryption(conn, media_id, encryption)
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


async def _cleanup_upload_session_unlocked(upload_id: str, *, allow_unindexed_local: bool = False, expired_only: bool = False) -> bool:
    """Release a reservation only after its possible physical object is gone."""
    from app.services import media_publication_recovery as publication
    if publication.pending(MEDIA_ROOT):
        return False
    try:
        row = await resource_pool.upload_session(upload_id, node_state.database)
    except p.ProtocolError:
        return True
    if row["state"] == "complete":
        return True
    if row["state"] != "reserved":
        return False
    if expired_only and not resource_pool.upload_expired(row):
        return False

    if row["member_kind"] == "MasterLocal":
        target = MEDIA_ROOT
        for part in row["media_path"].split("/"):
            target = target / part
            if target.is_symlink():
                return False
        target = target.resolve()
        if MEDIA_ROOT not in target.parents:
            return False
        exists = target.is_file()
        indexed = await _local_object_exists(row) if exists else False
        if exists and not (indexed or allow_unindexed_local):
            return False
        locator = resource_pool.path_locator(row["media_path"])
        async with node_state.database.connect() as conn:
            owned = await conn.scalar(select(s.global_media.c.media_id).where(s.global_media.c.path_locator == locator))
            indexed_owner = await conn.scalar(text("SELECT media_id FROM media_objects WHERE path_locator=:locator"), {"locator": locator})
        if owned or indexed_owner and indexed_owner != row["media_id"]:
            return False
        if expired_only and exists and target.stat().st_size == int(row["expected_bytes"]):
            encrypted = await _encrypted_upload(row["media_id"])
            if bool(row.get("content_encrypted", 0)) != encrypted:
                return False
            with target.open("rb") as source:
                head = source.read(4096)
            if encrypted or SIGNATURES.get(target.suffix.lower(), lambda _data: False)(head):
                from app.services.upload_claims import digest_file
                proof = await digest_file(target)
                with target.open('rb') as source:
                    os.fsync(source.fileno())
                publication.sync_hierarchy(MEDIA_ROOT, target.parent)
                await resource_pool.finalize_upload(upload_id, object_id=row["media_id"],
                    actual_size=proof["size_bytes"], etag=proof["etag"], database=node_state.database,
                    allow_expired=True, register_local=True)
                await _invalidate_after_upload()
                return True
        temporary = MEDIA_ROOT / f".cluster-upload-{upload_id}.part"
        if temporary.is_symlink():
            return False
        if exists:
            target.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        async with node_state.database.begin() as conn:
            await conn.execute(text("DELETE FROM media_objects WHERE media_id=:id"), {"id": row["media_id"]})
        await resource_pool.fail_upload(upload_id, node_state.database)
        return True

    member, relation = await _member_relation(row["storage_member_id"])
    if not member or not relation or relation.get("state") != "active" or relation.get("status") == "offline":
        return False
    try:
        result = await node_runtime.call(
            relation,
            f"/internal/v1/storage/{row['media_id']}/reconcile",
            {"path": row["media_path"], "expected_bytes": int(row["expected_bytes"]),
             "expired_only": expired_only, "encrypted": await _encrypted_upload(row["media_id"])},
        )
    except (p.ProtocolError, httpx.HTTPError):
        return False
    if expired_only and result.get("state") == "complete" and result.get("object_id") == row["media_id"]:
        if result.get("size_bytes") != int(row["expected_bytes"]) or not isinstance(result.get("etag"), str):
            return False
        await resource_pool.finalize_upload(upload_id, object_id=row["media_id"], actual_size=result["size_bytes"],
            etag=result["etag"], database=node_state.database, allow_expired=True)
        await _invalidate_after_upload()
        return True
    if result.get("object_id") != row["media_id"] or result.get("cleared") is not True or result.get("state") != "cleared":
        return False
    await resource_pool.fail_upload(upload_id, node_state.database)
    return True


async def _cleanup_upload_session(upload_id: str, *, allow_unindexed_local: bool = False, expired_only: bool = False) -> bool:
    from app.services.upload_claims import claim, UploadBusy
    try:
        if getattr(node_state,'_s3_configured',False):
            from app.services import s3_schema
            async with node_state.database.connect() as conn:
                is_s3 = await conn.scalar(select(s3_schema.uploads.c.session_id).where(s3_schema.uploads.c.session_id == upload_id))
            if is_s3:
                from app.services.s3_uploads import Uploads
                service = Uploads(node_state)
                result = await service.reconcile(upload_id) if expired_only else await service.cancel(upload_id,actor='admin')
                return result['state'] in ('published','aborted')
        with claim(upload_id):
            return await _cleanup_upload_session_unlocked(upload_id, allow_unindexed_local=allow_unindexed_local, expired_only=expired_only)
    except UploadBusy:
        return False


async def _cleanup_expired_uploads(limit: int = 50, time_budget=2.0) -> int:
    from app.core.config import settings
    now = int(time.time())
    async with node_state.database.connect() as conn:
        query = select(s.upload_sessions.c.upload_id).where(
            s.upload_sessions.c.state == "reserved",
            ((s.upload_sessions.c.updated_at <= now - settings.ADMIN_UPLOAD_INACTIVITY_TIMEOUT) | (s.upload_sessions.c.expires_at <= now)),
        )
        if getattr(node_state,'_s3_configured',False):
            from app.services import s3_schema
            query = query.where(~select(s3_schema.uploads.c.session_id).where(s3_schema.uploads.c.session_id == s.upload_sessions.c.upload_id).exists())
        upload_ids = list((await conn.execute(query.order_by(s.upload_sessions.c.created_at).limit(limit))).scalars())
    cleaned = 0
    try:
        async with asyncio.timeout(time_budget):
            for upload_id in upload_ids:
                try:
                    cleaned += int(await _cleanup_upload_session(str(upload_id), allow_unindexed_local=True, expired_only=True))
                except Exception as exc:
                    logger.warning("Upload recovery deferred upload_id=%s error=%s", upload_id, type(exc).__name__)
    except TimeoutError:
        pass
    return cleaned


async def _invalidate_after_upload():
    try:
        await invalidate_media_catalog()
    except Exception as exc:
        logger.warning("Upload committed; catalog invalidation deferred error=%s", type(exc).__name__)


async def _hidden_paths() -> set[str]:
    async with node_state.database.connect() as conn:
        result = await conn.execute(text("SELECT relative_path FROM media_visibility WHERE hidden=1"))
        return {str(row[0]) for row in result.fetchall()}


def _path_hidden(path: str, hidden: set[str]) -> bool:
    parts = path.split("/")
    return any("/".join(parts[:index]) in hidden for index in range(1, len(parts) + 1))


async def _tree_storage_modes(items: list[dict]) -> list[dict]:
    files = [item for item in items if item['kind'] == 'file']
    identifiers = [item['media_id'] for item in files]
    from app.services import s3_schema
    async with node_state.database.connect() as conn:
        descriptors = await media_crypto.encryption_for_ids(conn, identifiers)
        for offset in range(0, len(identifiers), 500):
            rows = (await conn.execute(select(s3_schema.objects.c.media_id,
                s3_schema.objects.c.content_encrypted, s3_schema.objects.c.size_bytes).where(
                    s3_schema.objects.c.media_id.in_(identifiers[offset:offset+500]),
                    s3_schema.objects.c.state == 'active'))).mappings()
            for row in rows:
                descriptor = descriptors.get(row['media_id'])
                if (bool(descriptor) != bool(row['content_encrypted'])
                        or descriptor is not None and descriptor.ciphertext_size != row['size_bytes']):
                    raise media_crypto.MetadataError('S3 encryption descriptor missing or inconsistent')
    for item in files:
        item['encrypted'] = item['media_id'] in descriptors
    return items


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
        return {"path": path, "query": query, "items": await _tree_storage_modes(matches[:legacy_admin.media_search.MAX_SEARCH_RESULTS]),
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
            "items": await _tree_storage_modes(sorted(items.values(), key=lambda item: (item["kind"] != "directory", item["name"].casefold())))}


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
    path = await _upload_logical_path(payload)
    try:
        from app.services import media_crypto
        metadata = media_crypto.upload_encryption(payload.storage_mode, payload.encryption, payload.preparation_token, "admin:" + session_hash)
        if metadata is not None and metadata.ciphertext_size != payload.size_bytes:
            raise p.ProtocolError("Ciphertext reservation size mismatch")
        plaintext_size = metadata.plaintext_size if metadata is not None else payload.size_bytes
        if plaintext_size > legacy_admin.settings.ADMIN_MAX_UPLOAD_FILE_SIZE:
            raise HTTPException(413, "文件超过单文件上传限制")
        await _cleanup_expired_uploads()
        reservation = await _reserve_upload(path, payload.size_bytes, _preferred_member(payload.storage_member_id), metadata)
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
                reservation["media_id"], "upload", path, payload.size_bytes, int(time.time()), encrypted=metadata is not None,
            )
            result["upload_url"] = relation["peer_endpoint"] + f"/internal/v1/storage/{reservation['media_id']}?token={token}"
        await admin_service.audit(session_hash, "upload-reserved", 1, path, "success",
                                  member["member_id"], request)
        return result
    except media_crypto.MetadataError as exc:
        raise HTTPException(400, str(exc)) from exc
    except p.ProtocolError as exc:
        raise HTTPException(409, str(exc)) from exc


async def _receive_master_local(upload_id: str, request: Request, row: dict, *, audit=None) -> dict:
    from app.services import media_manager, media_publication_recovery as publication
    destination = media_manager.resolve_safe_path(MEDIA_ROOT, row['media_path'])
    if MEDIA_ROOT not in destination.parents or destination.exists():
        raise HTTPException(409, "目标位置已存在同名文件")
    temporary = (MEDIA_ROOT / f".cluster-upload-{upload_id}.part").resolve()
    written, digest, head, published = 0, hashlib.sha256(), b"", False
    try:
        with temporary.open("xb") as output:
            from app.services.upload_claims import chunks
            async for chunk in chunks(request):
                if len(head) < 4096:
                    head += chunk[:4096 - len(head)]
                written += len(chunk)
                if written > int(row["expected_bytes"]):
                    raise HTTPException(413, "上传内容超过预留大小")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if written != int(row["expected_bytes"]):
            raise HTTPException(400, "上传内容大小与预留不一致")
        from app.services import media_crypto
        async with node_state.database.connect() as conn:
            encryption = await media_crypto.encryption_for_id(conn, row["media_id"])
        if encryption is not None and encryption.ciphertext_size != written:
            raise p.ProtocolError("Ciphertext size does not match reservation")
        if encryption is None and not SIGNATURES.get(destination.suffix.lower(), lambda _data: False)(head):
            raise HTTPException(400, "媒体内容与扩展名不匹配")
        async def publish_and_finalize():
            nonlocal published
            async with media_manager.media_mutation_lock:
                media_manager.ensure_media_mutations_ready()
                if destination.exists():
                    raise HTTPException(409, '目标位置已存在同名文件')
                journal, committed = None, False
                try:
                    journal = publication.create(MEDIA_ROOT, row['media_path'], temporary, None,
                        encryption, size=written, digest=digest.hexdigest(), reservation=row)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary.chmod(0o644)
                    # A reservation never authorizes overwriting an existing inode.
                    os.link(temporary, destination)
                    published = True
                    publication.sync_hierarchy(MEDIA_ROOT, destination.parent)
                    await resource_pool.finalize_upload(upload_id, object_id=row['media_id'], actual_size=written,
                        etag='"' + digest.hexdigest() + '"', database=node_state.database,
                        allow_expired=True, register_local=True, publication_record=journal, audit=audit)
                    committed = True
                    temporary.unlink()
                    publication.sync_directory(MEDIA_ROOT)
                    publication.finish(MEDIA_ROOT, journal)
                except BaseException as exc:
                    if committed or isinstance(exc, publication.PublicationCommitUnknown):
                        media_manager._RECOVERY_REQUIRED_ROOTS.add(MEDIA_ROOT)
                        raise
                    try:
                        if published:
                            destination.unlink()
                            publication.sync_hierarchy(MEDIA_ROOT, destination.parent)
                            published = False
                        if journal is not None:
                            publication.finish(MEDIA_ROOT, journal)
                    except BaseException:
                        media_manager._RECOVERY_REQUIRED_ROOTS.add(MEDIA_ROOT)
                        raise
                    raise
            await _invalidate_after_upload()
            return {'path': row['media_path'], 'media_id': row['media_id']}

        worker = asyncio.create_task(publish_and_finalize())
        await media_manager._finish_media_cleanup(worker)
        return worker.result()
    finally:
        temporary.unlink(missing_ok=True)
        publication.sync_directory(MEDIA_ROOT)



async def _encrypted_upload(identifier):
    from app.services import media_crypto
    async with node_state.database.connect() as conn:
        return await media_crypto.encryption_for_id(conn, identifier) is not None

@router.put("/upload/session/{upload_id}/bytes")
async def upload_session_bytes(upload_id: str, request: Request,
                               session_hash: str = Depends(require_session)):
    allow_unindexed_local = False
    from app.services.upload_claims import claim, UploadBusy
    try:
        with claim(upload_id):
            try:
                row = await resource_pool.upload_session(upload_id, node_state.database)
                if row["state"] != "reserved" or row["expires_at"] <= int(time.time()):
                    raise p.ProtocolError("Upload reservation is unavailable")
                if row["member_kind"] == "MasterLocal":
                    audit = _mutation_audit(session_hash, 'upload-finalized', [row['media_path']], request)
                    result = await _receive_master_local(upload_id, request, row, audit=audit)
                    return result
                relation = await node_state.relationship(row["relationship_id"])
                token = p.storage_token(
                    node_state.unseal(relation["credential"]), relation["relationship_id"],
                    node_state.node["node_id"], row["storage_member_id"], row["media_id"], row["media_id"],
                    "upload", row["media_path"], int(row["expected_bytes"]), int(time.time()), encrypted=await _encrypted_upload(row["media_id"]),
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
                    etag=str(result["etag"]), database=node_state.database, allow_expired=True,
                )
                await _invalidate_after_upload()
                await admin_service.audit(session_hash, "upload-finalized", 1, row["media_path"], "success",
                                          row["storage_member_id"], request)
                return {"path": media["media_path"], "media_id": media["media_id"]}
            except asyncio.CancelledError:
                from app.services.media_manager import _finish_media_cleanup
                cleanup = asyncio.create_task(_cleanup_upload_session_unlocked(
                    upload_id, allow_unindexed_local=allow_unindexed_local))
                try:
                    await _finish_media_cleanup(cleanup)
                except Exception as cleanup_error:
                    logger.warning('Cancelled upload cleanup deferred upload_id=%s error=%s',
                        upload_id, type(cleanup_error).__name__)
                raise
            except Exception as exc:
                cleaned = await _cleanup_upload_session_unlocked(upload_id, allow_unindexed_local=allow_unindexed_local)
                if not cleaned:
                    await admin_service.audit(session_hash, "upload-cleanup-deferred", 1, upload_id, "pending", "", request)
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(409, str(exc) if isinstance(exc, p.ProtocolError) else "存储上传失败") from exc
    except UploadBusy as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/upload/session/{upload_id}/finalize")
async def finalize_direct_upload(upload_id: str, request: Request,
                                 session_hash: str = Depends(require_session)):
    from app.services.upload_claims import claim, UploadBusy
    try:
        with claim(upload_id):
            try:
                row = await resource_pool.upload_session(upload_id, node_state.database)
                if row["transport"] != "Direct" or not row["relationship_id"]:
                    raise p.ProtocolError("Upload session is not Direct")
                relation = await node_state.relationship(row["relationship_id"])
                result = await node_runtime.call(
                    relation, f"/internal/v1/storage/{row['media_id']}/stat", {
                        "path": row["media_path"], "expected_bytes": int(row["expected_bytes"]),
                        "encrypted": await _encrypted_upload(row["media_id"]),
                    },
                )
                media = await resource_pool.finalize_upload(
                    upload_id, object_id=result["object_id"], actual_size=int(result["size_bytes"]),
                    etag=str(result["etag"]), database=node_state.database, allow_expired=True,
                )
                await _invalidate_after_upload()
                await admin_service.audit(session_hash, "upload-finalized", 1, row["media_path"], "success",
                                          row["storage_member_id"], request)
                return {"path": media["media_path"], "media_id": media["media_id"]}
            except Exception as exc:
                cleaned = await _cleanup_upload_session_unlocked(upload_id)
                if not cleaned:
                    await admin_service.audit(session_hash, "upload-cleanup-deferred", 1, upload_id, "pending", "", request)
                raise HTTPException(409, str(exc) if isinstance(exc, p.ProtocolError) else "Direct 上传校验失败") from exc
    except UploadBusy as exc:
        raise HTTPException(409, str(exc)) from exc


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
    s3_rows = [row for row in rows if row.get('transport') == 'S3']
    if s3_rows:
        if not exact_single or len(rows) != 1:
            raise HTTPException(409,'S3 批量下载请使用浏览器下载计划')
        from app.services.s3_objects import Objects
        from app.services.s3_signing import S3Error
        try:
            grant = await Objects(node_state).grant(rows[0]['resource_id'],method=request.method,download=True)
        except S3Error as error:
            raise HTTPException(409,'S3 下载授权失败，请检查节点状态') from error
        await admin_service.audit(session_hash,'download',1,rows[0]['path'],'success','s3_direct',request)
        return RedirectResponse(grant.url,status_code=307,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
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
