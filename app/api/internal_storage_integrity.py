"""Follower upload leases, durable quota and authenticated reconciliation."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, insert, select, text, update

from app.api import internal_nodes as legacy_internal
from app.services import resource_pool
from app.services.federation import protocol as p
from app.services.federation import schema as s
from app.services.federation.state import state
from app.services.media_manager import MEDIA_ROOT, MediaManager, SIGNATURES
from app.services.media_publication_recovery import sync_hierarchy
from app.services.upload_claims import UploadBusy, claim, chunks, digest_file

router = APIRouter()
logger = logging.getLogger("frontiercloud.storage.upload")


def session_id(original: str) -> str:
    return hashlib.sha256(("frontiercloud:follower-upload:v1:" + original).encode()).hexdigest()[:32]


def _target(path: str):
    resource_pool.validate_media_path(path)
    target = MEDIA_ROOT
    for part in path.split("/"):
        target = target / part
        if target.is_symlink():
            raise HTTPException(400, "Storage path cannot contain symlinks")
    target = target.resolve()
    if MEDIA_ROOT.resolve() not in target.parents:
        raise HTTPException(400, "Storage path is outside the media root")
    return target


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def _session(original: str, conn=None):
    if conn is None:
        async with state.database.connect() as owned:
            return await _session(original, owned)
    row = (await conn.execute(select(s.upload_sessions).where(s.upload_sessions.c.upload_id == session_id(original)).with_for_update())).mappings().first()
    return dict(row) if row else None


def _same_upload(row, original, path, expected):
    if row and (row["media_id"] != original or row["media_path"] != path
                or int(row["expected_bytes"]) != expected or row["storage_member_id"] != state.node["node_id"]):
        raise HTTPException(409, "Durable storage reservation identity mismatch")


async def _reserve(original, path, expected, *, encrypted=False):
    now = int(time.time())
    async with state.database.begin() as conn:
        row = await _session(original, conn)
        _same_upload(row, original, path, expected)
        if row and bool(row["content_encrypted"]) != encrypted:
            raise HTTPException(409, "Signed encryption mode differs from the durable upload")
        if row:
            if row["state"] == "complete":
                return "complete"
            raise HTTPException(409, "Storage upload identity was already consumed; create a new upload session")
        if _target(path).exists():
            raise HTTPException(409, "Storage target already exists")
        if await conn.scalar(text("SELECT media_id FROM media_objects WHERE path_locator=:locator"), {"locator": resource_pool.path_locator(path)}):
            raise HTTPException(409, "Storage path already has a durable owner")
        member = (await conn.execute(select(s.storage_members).where(s.storage_members.c.member_id == state.node["node_id"]).with_for_update())).mappings().first()
        physical = resource_pool.physical_free(MEDIA_ROOT) - resource_pool.PHYSICAL_RESERVE_BYTES
        logical = int(member["allocated_bytes"]) - int(member["used_bytes"]) - int(member["reserved_bytes"]) if member else 0
        if not member or not member["storage_enabled"] or not member["writable"] or min(physical, logical) < expected:
            raise HTTPException(507, "Follower storage is not writable or lacks capacity")
        await conn.execute(insert(s.upload_sessions).values(
            upload_id=session_id(original), storage_member_id=state.node["node_id"], media_id=original,
            media_path=path, path_locator=resource_pool.path_locator(path), object_kind="audio" if path.startswith("music/") else "video",
            expected_bytes=expected, content_encrypted=int(encrypted), state="reserved", expires_at=now + resource_pool.UPLOAD_TTL_SECONDS, created_at=now, updated_at=now,
        ))
        await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == state.node["node_id"]).values(
            reserved_bytes=s.storage_members.c.reserved_bytes + expected, physical_free_bytes=resource_pool.physical_free(MEDIA_ROOT), updated_at=now,
        ))
    return "reserved"


async def _complete(original, path, expected):
    now = int(time.time())
    async with state.database.begin() as conn:
        row = await _session(original, conn)
        _same_upload(row, original, path, expected)
        if not row or row["state"] != "reserved":
            raise HTTPException(409, "Storage reservation was cancelled or already completed")
        timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
        await conn.execute(text("""INSERT INTO media_objects
            (media_id,object_kind,media_path,path_locator,created_at,updated_at)
            VALUES (:id,:kind,:path,:locator,:now,:now)"""), {
                "id": original, "kind": row["object_kind"], "path": path,
                "locator": resource_pool.path_locator(path), "now": timestamp,
            })
        await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == state.node["node_id"]).values(
            reserved_bytes=func.greatest(0, s.storage_members.c.reserved_bytes - expected),
            used_bytes=s.storage_members.c.used_bytes + expected,
            physical_free_bytes=resource_pool.physical_free(MEDIA_ROOT), updated_at=now,
        ))
        await conn.execute(update(s.upload_sessions).where(s.upload_sessions.c.upload_id == session_id(original)).values(state="complete", path_locator=None, updated_at=now))


async def _reconcile_unlocked(original, path, expected, *, expired_only=False):
    """The caller holds the same kernel lease as the byte writer."""
    target = _target(path)
    temporary = MEDIA_ROOT / f".cluster-upload-{original}.part"
    row = await _session(original)
    _same_upload(row, original, path, expected)
    if expired_only and row and row["state"] in {"reserved", "complete"} and target.is_file():
        try:
            return await _proof(original, path, expected, encrypted=bool(row["content_encrypted"]))
        except HTTPException as exc:
            if exc.status_code not in {404, 409}:
                raise
    async with state.database.connect() as conn:
        owner = (await conn.execute(text("SELECT media_id,media_path FROM media_objects WHERE path_locator=:locator"), {"locator": resource_pool.path_locator(path)})).mappings().first()
    if owner and (owner["media_id"] != original or owner["media_path"] != path):
        return {"object_id": original, "state": "conflict", "cleared": False}
    if target.exists() and not (owner or row and row["state"] in {"reserved", "complete"}):
        return {"object_id": original, "state": "unknown_owner", "cleared": False}
    if temporary.is_symlink() or target.exists() and not target.is_file():
        return {"object_id": original, "state": "invalid_path", "cleared": False}
    temporary.unlink(missing_ok=True)
    if target.exists():
        target.unlink()
        _sync_directory(target.parent)
    _sync_directory(MEDIA_ROOT)
    now = int(time.time())
    async with state.database.begin() as conn:
        locked = await _session(original, conn)
        _same_upload(locked, original, path, expected)
        reserved = expected if locked and locked["state"] == "reserved" else 0
        used = expected if locked and locked["state"] == "complete" else (expected if owner and not locked else 0)
        await conn.execute(text("DELETE FROM media_objects WHERE media_id=:id AND media_path=:path"), {"id": original, "path": path})
        if reserved or used:
            await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == state.node["node_id"]).values(
                reserved_bytes=func.greatest(0, s.storage_members.c.reserved_bytes - reserved),
                used_bytes=func.greatest(0, s.storage_members.c.used_bytes - used),
                physical_free_bytes=resource_pool.physical_free(MEDIA_ROOT), updated_at=now,
            ))
        if locked:
            await conn.execute(update(s.upload_sessions).where(s.upload_sessions.c.upload_id == session_id(original)).values(state="cancelled", path_locator=None, updated_at=now))
        else:
            # A durable tombstone rejects a previously-issued browser token
            # arriving after Master released its reservation and the lease.
            await conn.execute(insert(s.upload_sessions).values(
                upload_id=session_id(original), storage_member_id=state.node["node_id"], media_id=original,
                media_path=path, path_locator=None, object_kind="audio" if path.startswith("music/") else "video",
                expected_bytes=expected, state="cancelled", expires_at=now + resource_pool.UPLOAD_TTL_SECONDS,
                created_at=now, updated_at=now,
            ))
    return {"object_id": original, "state": "cleared", "cleared": True}


async def _proof(original, path, expected=None, *, encrypted=False):
    target = _target(path)
    row = await _session(original)
    if row:
        _same_upload(row, original, path, int(row["expected_bytes"]) if expected is None else expected)
        if row["state"] not in {"reserved", "complete"}:
            raise HTTPException(404, "Storage upload has been cancelled")
    async with state.database.connect() as conn:
        owner = await conn.scalar(text("SELECT media_id FROM media_objects WHERE path_locator=:locator AND media_path=:path"), {"locator": resource_pool.path_locator(path), "path": path})
    if owner and owner != original:
        raise HTTPException(409, "Storage path has a different durable owner")
    if not target.is_file() or not row and owner != original:
        raise HTTPException(404, "Storage object not found")
    if row and bool(row["content_encrypted"]) != encrypted:
        raise HTTPException(409, "Encryption mode differs from the durable storage reservation")
    result = await digest_file(target)
    if (expected is not None and result["size_bytes"] != expected
            or row and result["size_bytes"] != int(row["expected_bytes"])):
        raise HTTPException(409, "Storage object does not match its reservation size")
    if row and row["state"] == "reserved":
        if owner:
            raise HTTPException(409, "Indexed storage object has an inconsistent reservation")
        if not encrypted:
            with target.open("rb") as stream:
                head = stream.read(4096)
            if not SIGNATURES.get(target.suffix.lower(), lambda _data: False)(head):
                raise HTTPException(409, "Recovered plaintext content does not match its extension")
        # A dead writer may have published before syncing bytes or parent entries.
        # The durable reservation becomes complete only after both are persisted.
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        sync_hierarchy(MEDIA_ROOT.resolve(), target.parent)
        await _complete(original, path, int(row["expected_bytes"]))
    return {"object_id": original, "state": "complete", **result}


@router.put("/storage/{original}")
async def storage_upload(request: Request, original: str):
    relation, value = await legacy_internal.storage_capability(request, original, "upload")
    cors = legacy_internal.storage_cors(relation, request.headers.get("origin"))
    expected, path = int(value["size"]), value["path"]
    if expected <= 0:
        raise HTTPException(413, "Upload size is invalid")
    try:
        with claim(original):
            mode = await _reserve(original, path, expected, encrypted=value.get("encrypted", False))
            if mode == "complete":
                return JSONResponse(await _proof(original, path, expected, encrypted=value.get("encrypted", False)), headers=cors)
            target = _target(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = MEDIA_ROOT / f".cluster-upload-{original}.part"
            written, digest, head = 0, hashlib.sha256(), b""
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    async for chunk in chunks(request):
                        if len(head) < 4096:
                            head += chunk[:4096 - len(head)]
                        written += len(chunk)
                        if written > expected:
                            raise HTTPException(413, "Upload exceeds reserved size")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if written != expected:
                    raise HTTPException(400, "Upload size does not match reservation")
                if not value.get("encrypted", False) and not SIGNATURES.get(target.suffix.lower(), lambda _data: False)(head):
                    raise HTTPException(400, "媒体内容与扩展名不匹配")
                MediaManager._validate_media_destination(target)
                os.replace(temporary, target)
                sync_hierarchy(MEDIA_ROOT.resolve(), target.parent)
                await _complete(original, path, expected)
                return JSONResponse({"object_id": original, "state": "complete", "size_bytes": written, "sha256": digest.hexdigest(), "etag": f'"{digest.hexdigest()}"'}, headers=cors)
            except BaseException:
                try:
                    await _reconcile_unlocked(original, path, expected)
                except Exception as exc:
                    logger.warning("Storage upload cleanup deferred object_id=%s error=%s", original, type(exc).__name__)
                raise
    except UploadBusy as exc:
        raise HTTPException(409, str(exc)) from exc


async def _control(request, original):
    relation = await legacy_internal.authenticated(request)
    if (state.node["role"] != "Follower" or relation["direction"] != "upstream"
            or relation["state"] != "active" or not p.OBJECT_ID.fullmatch(original)):
        raise HTTPException(403, "Only an authenticated Follower relationship manages storage objects")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        path = str(value["path"])
        _target(path)
        expected = value.get("expected_bytes")
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or not 0 < expected <= 10 * 1024 ** 3):
            raise ValueError()
        if not isinstance(value.get("encrypted", False), bool):
            raise ValueError()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, p.ProtocolError) as exc:
        raise HTTPException(400, "Invalid storage control request") from exc
    if not isinstance(value.get("expired_only", False), bool):
        raise HTTPException(400, "Invalid reconciliation expiry mode")
    return path, expected, value.get("encrypted", False), value.get("expired_only", False)


@router.post("/storage/{original}/stat")
async def storage_stat(request: Request, original: str):
    path, expected, encrypted, _expired_only = await _control(request, original)
    try:
        with claim(original):
            return await _proof(original, path, expected, encrypted=encrypted)
    except UploadBusy as exc:
        raise HTTPException(409, "Storage upload still has a live writer") from exc


@router.post("/storage/{original}/reconcile")
async def storage_reconcile(request: Request, original: str):
    path, expected, _encrypted, expired_only = await _control(request, original)
    if expected is None:
        raise HTTPException(400, "Reconciliation requires the expected reservation size")
    try:
        with claim(original):
            return await _reconcile_unlocked(original, path, expected, expired_only=expired_only)
    except UploadBusy:
        return {"object_id": original, "state": "busy", "cleared": False}


@router.post("/storage/{original}/delete")
async def storage_delete(request: Request, original: str):
    _relation, value = await legacy_internal.storage_capability(request, original, "delete")
    try:
        with claim(original):
            result = await _reconcile_unlocked(original, value["path"], int(value["size"]))
            if not result["cleared"]:
                raise HTTPException(409, "Storage deletion cannot prove exclusive ownership")
            return {"status": "deleted"}
    except UploadBusy as exc:
        raise HTTPException(409, "Storage deletion found an active writer") from exc


async def cleanup_expired_storage(limit=50):
    if state.node.get("role") != "Follower":
        return 0
    from app.core.config import settings
    now = int(time.time())
    async with state.database.connect() as conn:
        rows = [dict(row) for row in (await conn.execute(select(s.upload_sessions).where(
            s.upload_sessions.c.state == "reserved", s.upload_sessions.c.storage_member_id == state.node["node_id"],
            ((s.upload_sessions.c.updated_at <= now - settings.ADMIN_UPLOAD_INACTIVITY_TIMEOUT) | (s.upload_sessions.c.expires_at <= now)),
        ).order_by(s.upload_sessions.c.updated_at).limit(limit))).mappings()]
    cleaned = 0
    for row in rows:
        try:
            with claim(row["media_id"]):
                result = await _reconcile_unlocked(row["media_id"], row["media_path"], int(row["expected_bytes"]), expired_only=True)
                cleaned += int(result.get("cleared") is True or result.get("state") == "complete")
        except UploadBusy:
            continue
    return cleaned


def install() -> None:
    legacy_internal.router.routes[:] = [route for route in legacy_internal.router.routes if not (
        getattr(route, "path", None) in {"/internal/v1/storage/{original}", "/internal/v1/storage/{original}/stat", "/internal/v1/storage/{original}/reconcile", "/internal/v1/storage/{original}/delete"}
        and ((getattr(route, "path", "").endswith("/stat") or getattr(route, "path", "").endswith("/reconcile") or getattr(route, "path", "").endswith("/delete"))
             or "PUT" in (getattr(route, "methods", None) or set()))
    )]
    legacy_internal.router.include_router(router)
