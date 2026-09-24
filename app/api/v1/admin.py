import json
import hashlib
import os
import ssl
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    Query,
)
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import httpx
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.db import engine
from app.core.static_assets import static_asset_url
from app.services import admin_service
from app.services import ip_security
from app.services import lyrics
from app.services import network_observation
from app.services import media_search, playback
from app.services.federation import routing as node_routing
from app.services.federation import protocol as p
from app.services.federation.catalog import catalog as node_catalog
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import MEDIA_ROOT, MediaManager, SIGNATURES


router = APIRouter()


class MediaPriorityChange(BaseModel):
    media_path: str = Field(min_length=1, max_length=1024)
    resource_id: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    value: int = Field(ge=-7, le=500)


class UploadReservation(BaseModel):
    storage_member_id: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")
    target_dir: str = Field(max_length=1024)
    relative_path: str | None = Field(None, max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=10 * 1024 ** 3)


def _media_priority_scope(value: str) -> str:
    scope = str(value or "").strip().replace("\\", "/").strip("/")
    if not scope:
        return ""
    parts = scope.split("/")
    if parts[0] not in {"music", "vido"} or any(
        not part or part in {".", ".."} for part in parts
    ):
        raise ValueError("媒体优先级目录无效")
    return "/".join(parts)


def _classify_media_priority(
    items: list[dict], scope: str, normalized_query: str,
) -> tuple[list[dict], list[dict], int]:
    prefix = f"{scope}/" if scope else ""
    scoped = [
        item for item in items
        if not scope or str(item["media_path"]).startswith(prefix)
    ]
    directory_counts: dict[str, int] = {}
    direct_items: list[dict] = []
    for item in scoped:
        media_path = str(item["media_path"])
        remainder = media_path[len(prefix):]
        if "/" in remainder:
            child = remainder.split("/", 1)[0]
            child_path = f"{scope}/{child}" if scope else child
            directory_counts[child_path] = directory_counts.get(child_path, 0) + 1
        else:
            direct_items.append(item)
    directories = [
        {"name": path.rsplit("/", 1)[-1], "path": path, "count": count}
        for path, count in directory_counts.items()
    ]
    directories.sort(key=lambda item: (item["name"].casefold(), item["path"].casefold()))
    if normalized_query:
        visible_items = [item for item in scoped if media_search.matches_search(
            media_search.build_search_text(item["title"], item["media_path"]),
            normalized_query,
        )]
    else:
        visible_items = direct_items
    return directories, visible_items, len(scoped)


async def require_session(request: Request) -> str:
    return await admin_service.require_admin(request)


def _mutation_audit(session_hash: str, action: str, paths: list[str], request: Request):
    """Keep all targets, split into bounded rows, and share business transactions."""
    targets = list(paths)

    async def write(conn, result: str, count: int, detail: dict):
        nonlocal targets
        detail = dict(detail)
        manifest = detail.pop("manifest", None)
        if manifest is not None:
            targets = [item["relative_path"] for item in manifest]
        if detail.get("path"):
            targets = [detail["path"]]
        detail["affected_total"] = count
        # A path has at most the supported depth and filename lengths; packing
        # by serialized length avoids silently truncating large batch evidence.
        batches, batch = [], []
        for path in targets:
            if batch and len(json.dumps(batch + [path], ensure_ascii=False)) > 8000:
                batches.append(batch)
                batch = []
            batch.append(path)
        batches.append(batch)
        for batch in batches:
            await admin_service.audit(
                session_hash, action, len(batch), json.dumps(batch, ensure_ascii=False),
                result, json.dumps(detail, ensure_ascii=False), request, conn=conn,
            )

    return write


def secure_admin_transport(request: Request) -> bool:
    # Direct loopback HTTP is allowed only for the local IDE workflow.
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    direct_scheme = request.url.scheme.lower()
    client_host = request.client.host if request.client else ""
    return forwarded_proto == "https" or direct_scheme == "https" or client_host in {"127.0.0.1", "::1"}


# ============================================================
# 1. Persistent or one-time Admin Key to admin session
# ============================================================

@router.post("/elevate")
async def elevate(
    request: Request,
    response: Response,
    token: str = Form(...),
):
    credential = await admin_service.redeem_admin_credential(
        token,
        request,
    )

    await admin_service.create_session(
        credential.key_hash,
        request,
        response,
        idle_ttl=credential.idle_ttl,
        credential_kind=credential.kind,
    )

    return {
        "status": "ok",
        "redirect": "/api/v1/media/admin",
    }


# ============================================================
# 2. Admin page
#
# The admin router is already mounted by endpoints.py at:
#
# /api/v1/media/admin
#
# Do not append another /admin segment here.
# ============================================================

@router.get(
    "",
    response_class=HTMLResponse,
    include_in_schema=False,
)
@router.get(
    "/",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def admin_page(
    request: Request,
    session_hash: str = Depends(require_session),
):
    path = (
        Path(__file__).resolve().parents[3]
        / "static"
        / "media"
        / "admin.html"
    )

    if not path.exists():
        raise HTTPException(
            status_code=500,
            detail="Admin 页面文件不存在",
        )

    content = path.read_text(encoding="utf-8")
    for marker, asset in {
        "{{ADMIN_CSS_URL}}": "css/admin.css",
        "{{ADMIN_JS_URL}}": "js/admin.js",
        "{{NODES_JS_URL}}": "js/nodes.js",
    }.items():
        content = content.replace(marker, static_asset_url(asset))
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-store",
        },
    )


# ============================================================
# 3. Admin session status
# ============================================================

@router.get("/status")
async def admin_status(
    request: Request,
    session_hash: str = Depends(require_session),
):
    from app.services.federation.state import state as node_state
    master_url = ""
    if node_state.node["role"] == "Follower":
        relationships = await node_state.list_relationships()
        upstream = next((row for row in relationships if row["direction"] == "upstream"
                         and row["state"] in ("pending", "active")), None)
        master_url = upstream["peer_endpoint"] if upstream else ""
    return {
        "status": "ok",
        "session": True,
        "node_role": node_state.node["role"],
        "master_url": master_url,
        "limits": {
            "max_upload_file_size": settings.ADMIN_MAX_UPLOAD_FILE_SIZE,
            "max_upload_task_files": settings.ADMIN_MAX_UPLOAD_TASK_FILES,
            "max_batch_files": settings.ADMIN_MAX_BATCH_FILES,
            "max_lyric_file_size": MediaManager.LYRIC_MAX_BYTES,
        },
        "csrf_cookie_name": settings.ADMIN_CSRF_COOKIE_NAME,
        "credential_kind": request.scope.get("admin_credential_kind", "persistent"),
        "session_idle_minutes": int(
            request.scope.get("admin_session_ttl", settings.ADMIN_SESSION_TTL)
        ) // 60,
    }


@router.get("/media-priority")
async def media_priority(
    request: Request,
    q: str = Query("", max_length=100),
    media_type: str = Query("", pattern=r"^(|audio|video)$"),
    path: str = Query("", max_length=1024),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
    session_hash: str = Depends(require_session),
):
    from app.services.federation.state import state as node_state
    if node_state.node["role"] == "Master":
        # The global catalog already includes Master Local placements.  Mixing
        # a local filesystem scan into it would duplicate those media objects.
        items = [node_routing.item(row) for row in await node_catalog.resources()]
    else:
        items = await playback.attach_stats_and_sort(
            await MediaManager.list_media_objects(), "admin-media-priority"
        )
    if media_type:
        items = [item for item in items if item["type"] == media_type]
    try:
        scope = _media_priority_scope(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    normalized_query = media_search.normalized_query(q) if q else ""
    directories, items, catalog_total = _classify_media_priority(
        items, scope, normalized_query,
    )
    items.sort(key=lambda item: (
        -int(item.get("preference", 0)),
        str(item["media_path"]).casefold(),
        str(item["media_id"]),
    ))
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size],
        "directories": directories,
        "scope": scope,
        "parent": scope.rsplit("/", 1)[0] if "/" in scope else "",
        "catalog_total": catalog_total,
        "searching": bool(normalized_query),
        "minimum": playback.MIN_PREFERENCE,
        "maximum": playback.MAX_PREFERENCE,
        "pagination": {"page": page, "pages": pages, "total": total, "page_size": page_size},
    }


@router.post("/media-priority")
async def update_media_priority(
    payload: MediaPriorityChange,
    request: Request,
    session_hash: str = Depends(require_session),
):
    audit = _mutation_audit(session_hash, "media_priority", [payload.media_path], request)
    try:
        if payload.resource_id:
            return await node_routing.mutate_stats(
                payload.resource_id, payload.media_path, preference=payload.value, audit=audit,
            )
        return await playback.set_preference(
            MEDIA_ROOT, payload.media_path, payload.value, audit=audit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ============================================================
# 4. Admin file tree
# ============================================================

@router.get("/tree")
async def admin_tree(
    request: Request,
    path: str = "",
    session_hash: str = Depends(require_session),
):
    from app.services.federation.state import state as node_state
    if node_state.node["role"] == "Master" and path and path.split("/", 1)[0] in {"music", "vido"}:
        scope = _media_priority_scope(path)
        parts = scope.split("/")
        if len(parts) > 3:
            raise HTTPException(400, "目录不在受支持的媒体层级内")
        prefix = scope.rstrip("/") + "/"
        rows = await node_catalog.resources(directory=scope)
        items: dict[str, dict] = {}
        for row in rows:
            remainder = row["path"][len(prefix):]
            if "/" in remainder:
                name = remainder.split("/", 1)[0]
                child = prefix + name
                items.setdefault(child, {"name": name, "path": child, "kind": "directory",
                                         "size": None, "hidden": False, "media": False, "hideable": True})
            else:
                items[row["path"]] = {"name": remainder, "path": row["path"], "kind": "file",
                    "size": row["payload"]["size"], "hidden": False, "media": True, "hideable": False,
                    "media_id": row["resource_id"], "storage_member_id": row["owner_id"],
                    "transport": row.get("transport"), "node_health": row.get("health")}
        return {"path": scope, "items": sorted(items.values(), key=lambda item: (item["kind"] != "directory", item["name"].casefold()))}
    return await MediaManager.list_tree(path)


@router.get("/tree/search")
async def admin_tree_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=100),
    path: str = Query("", max_length=1024),
    session_hash: str = Depends(require_session),
):
    try:
        from app.services.federation.state import state as node_state
        if node_state.node["role"] == "Master" and path.split("/", 1)[0] in {"music", "vido"}:
            normalized = media_search.normalized_query(q)
            rows = await node_catalog.resources(directory=_media_priority_scope(path))
            matches = []
            for row in rows:
                if media_search.matches_search(media_search.build_search_text(
                        row["path"].rsplit("/", 1)[-1], row["path"]), normalized):
                    matches.append({"name": row["path"].rsplit("/", 1)[-1], "path": row["path"],
                        "kind": "file", "size": row["payload"]["size"], "hidden": False, "media": True,
                        "hideable": False, "media_id": row["resource_id"],
                        "storage_member_id": row["owner_id"], "transport": row.get("transport"),
                        "node_health": row.get("health")})
            return {"path": path, "query": q, "items": matches[:media_search.MAX_SEARCH_RESULTS],
                    "truncated": len(matches) > media_search.MAX_SEARCH_RESULTS}
        return await MediaManager.search_tree(q, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ============================================================
# 5. Single-file upload; browsers submit multi-file and folder jobs one file
# at a time so progress remains accurate.
# ============================================================

async def _upload_logical_path(payload: UploadReservation) -> str:
    """Validate a pool path without requiring its directory on the Master disk."""
    try:
        target_dir = MediaManager.normalize_relative(payload.target_dir)
        directory_parts = target_dir.split("/")
        if len(directory_parts) not in (2, 3) or directory_parts[0] not in {"music", "vido"}:
            raise HTTPException(400, "上传目录必须是 music/vido 下的分类目录或其一层子目录")
        if payload.relative_path:
            relative = MediaManager.normalize_relative(payload.relative_path)
            relative_parts = relative.split("/")
            name = MediaManager.validate_name(relative_parts[-1])
            logical = "/".join(directory_parts + relative_parts[:-1] + [name])
        else:
            logical = "/".join(directory_parts + [MediaManager.validate_name(payload.filename)])
        from app.services import resource_pool
        resource_pool.validate_media_path(logical)
    except p.ProtocolError as exc:
        raise HTTPException(400, str(exc)) from exc

    # One category uses either a flat layout or one nested directory level.
    category = "/".join(logical.split("/")[:2])
    existing = await node_catalog.resources(root=category)
    depth = len(logical.split("/"))
    if any(len(str(row["path"]).split("/")) != depth for row in existing):
        detail = ("该分类已使用子目录，禁止在分类目录直接上传媒体" if depth == 3
                  else "该分类已有直接媒体，禁止再使用子目录存放媒体")
        raise HTTPException(409, detail)
    return logical


@router.get("/storage-pool")
async def storage_pool(_session: str = Depends(require_session)):
    from app.services.federation.state import state as node_state
    from app.services import resource_pool
    if node_state.node["role"] != "Master":
        return {"members": [], "standalone": True}
    return await resource_pool.pool_summary(node_state.database)


@router.post("/upload/session")
async def create_upload_session(payload: UploadReservation, request: Request,
                                session_hash: str = Depends(require_session)):
    from app.services.federation.state import state as node_state
    from app.services import resource_pool
    if node_state.node["role"] != "Master":
        raise HTTPException(409, "Storage Pool uploads require a Master")
    if payload.size_bytes > settings.ADMIN_MAX_UPLOAD_FILE_SIZE:
        raise HTTPException(413, "文件超过单文件上传限制")
    path = await _upload_logical_path(payload)
    try:
        reservation = await resource_pool.reserve_upload(path, payload.size_bytes,
                                                          payload.storage_member_id, node_state.database)
        member = reservation["member"]
        result = {"upload_id": reservation["upload_id"], "media_id": reservation["media_id"],
                  "path": path, "transport": member["transport"], "member_id": member["member_id"],
                  "upload_url": f"/api/v1/media/admin/upload/session/{reservation['upload_id']}/bytes"}
        if member["transport"] == "Direct":
            relation = await node_state.relationship(member["relationship_id"])
            token = p.storage_token(node_state.unseal(relation["credential"]), relation["relationship_id"],
                node_state.node["node_id"], member["member_id"], reservation["media_id"],
                reservation["media_id"], "upload", path, payload.size_bytes, int(time.time()))
            result["upload_url"] = relation["peer_endpoint"] + f"/internal/v1/storage/{reservation['media_id']}?token={token}"
        await admin_service.audit(session_hash, "upload-reserved", 1, path, "success",
                                  member["member_id"], request)
        return result
    except p.ProtocolError as exc:
        raise HTTPException(409, str(exc)) from exc


async def _local_storage_receive(request: Request, row: dict) -> dict:
    destination = (MEDIA_ROOT / row["media_path"]).resolve()
    if MEDIA_ROOT not in destination.parents or destination.exists():
        raise HTTPException(409, "目标位置已存在同名文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = (MEDIA_ROOT / f".cluster-upload-{row['upload_id']}.part").resolve()
    written, digest, head = 0, hashlib.sha256(), b""
    try:
        with temporary.open("xb") as output:
            async for chunk in request.stream():
                if not head:
                    head = chunk[:4096]
                written += len(chunk)
                if written > int(row["expected_bytes"]):
                    raise HTTPException(413, "上传内容超过预留大小")
                digest.update(chunk); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        if written != int(row["expected_bytes"]):
            raise HTTPException(400, "上传内容大小与预留不一致")
        if not SIGNATURES.get(destination.suffix.lower(), lambda _data: False)(head):
            raise HTTPException(400, "媒体内容与扩展名不匹配")
        os.replace(temporary, destination)
        from datetime import datetime, timezone
        from app.core.db import engine
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO media_objects
                (media_id, object_kind, media_path, path_locator, created_at, updated_at)
                VALUES (:media_id, :kind, :path, :locator, :now, :now)
                ON DUPLICATE KEY UPDATE media_path=VALUES(media_path), updated_at=VALUES(updated_at)
            """), {"media_id": row["media_id"], "kind": row["object_kind"], "path": row["media_path"],
                     "locator": hashlib.sha256(row["media_path"].encode()).hexdigest(),
                     "now": datetime.now(timezone.utc).replace(tzinfo=None)})
        return {"object_id": row["media_id"], "size_bytes": written,
                "sha256": digest.hexdigest(), "etag": f'"{digest.hexdigest()}"'}
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@router.put("/upload/session/{upload_id}/bytes")
async def upload_session_bytes(upload_id: str, request: Request,
                               session_hash: str = Depends(require_session)):
    from app.services.federation.state import state as node_state
    from app.services import resource_pool
    try:
        row = await resource_pool.upload_session(upload_id, node_state.database)
        if row["state"] != "reserved" or row["expires_at"] <= int(time.time()):
            raise p.ProtocolError("Upload reservation is unavailable")
        if row["member_kind"] == "MasterLocal":
            result = await _local_storage_receive(request, row)
        else:
            relation = await node_state.relationship(row["relationship_id"])
            token = p.storage_token(node_state.unseal(relation["credential"]), relation["relationship_id"],
                node_state.node["node_id"], row["storage_member_id"], row["media_id"], row["media_id"],
                "upload", row["media_path"], int(row["expected_bytes"]), int(time.time()))
            headers = {"X-Storage-Capability": token, "Content-Type": "application/octet-stream"}
            timeout = httpx.Timeout(None, connect=10)
            async with httpx.AsyncClient(verify=ssl.create_default_context(), trust_env=False, timeout=timeout) as client:
                async with client.stream("PUT", relation["peer_endpoint"] + f"/internal/v1/storage/{row['media_id']}",
                                         headers=headers, content=request.stream()) as upstream:
                    payload = await upstream.aread()
                    if upstream.status_code != 200 or len(payload) > p.MAX_CONTROL_BYTES:
                        raise p.ProtocolError(f"Follower storage upload HTTP {upstream.status_code}")
                    result = json.loads(payload)
        media = await resource_pool.finalize_upload(upload_id, object_id=result["object_id"],
            actual_size=int(result["size_bytes"]), etag=str(result["etag"]), database=node_state.database)
        await invalidate_media_catalog()
        await admin_service.audit(session_hash, "upload-finalized", 1, row["media_path"], "success",
                                  row["storage_member_id"], request)
        return {"path": media["media_path"], "media_id": media["media_id"]}
    except Exception as exc:
        await resource_pool.fail_upload(upload_id, node_state.database)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(409, str(exc) if isinstance(exc, p.ProtocolError) else "存储上传失败") from exc


@router.post("/upload/session/{upload_id}/finalize")
async def finalize_direct_upload(upload_id: str, request: Request,
                                 session_hash: str = Depends(require_session)):
    from app.services.federation.state import state as node_state
    from app.services.federation.runtime import runtime as node_runtime
    from app.services import resource_pool
    try:
        row = await resource_pool.upload_session(upload_id, node_state.database)
        if row["transport"] != "Direct" or not row["relationship_id"]:
            raise p.ProtocolError("Upload session is not Direct")
        relation = await node_state.relationship(row["relationship_id"])
        result = await node_runtime.call(relation, f"/internal/v1/storage/{row['media_id']}/stat",
                                         {"path": row["media_path"]})
        media = await resource_pool.finalize_upload(upload_id, object_id=result["object_id"],
            actual_size=int(result["size_bytes"]), etag=str(result["etag"]), database=node_state.database)
        await invalidate_media_catalog()
        await admin_service.audit(session_hash, "upload-finalized", 1, row["media_path"], "success",
                                  row["storage_member_id"], request)
        return {"path": media["media_path"], "media_id": media["media_id"]}
    except Exception as exc:
        await resource_pool.fail_upload(upload_id, node_state.database)
        raise HTTPException(409, str(exc) if isinstance(exc, p.ProtocolError) else "Direct 上传校验失败") from exc

@router.post("/upload/item")
async def upload_item(
    request: Request,
    file: Annotated[
        UploadFile,
        File(...),
    ],
    target_dir: Annotated[
        str,
        Form(),
    ] = "",
    relative_path: Annotated[
        str | None,
        Form(),
    ] = None,
    session_hash: str = Depends(require_session),
):
    source = relative_path or file.filename or ""
    try:
        try:
            if relative_path:
                target, upload_name = MediaManager.folder_upload_target(target_dir, relative_path)
                file.filename = upload_name
            else:
                target = MediaManager.validate_destination_dir(target_dir)
            saved_path = await MediaManager.upload_one(file, target, audit=_mutation_audit(session_hash, "upload_item", [source], request))
        except HTTPException as exc:
            await admin_service.audit(session_hash, "upload_item", 1, source, "failed", str(exc.detail), request)
            raise

        await invalidate_media_catalog()
        return {"path": saved_path}
    finally:
        await file.close()


@router.post("/upload/lyric")
async def upload_lyric(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    session_hash: str = Depends(require_session),
):
    source = file.filename or ""
    try:
        try:
            saved_path = await MediaManager.upload_lyric(file, audit=_mutation_audit(session_hash, "upload_lyric", [source], request))
        except HTTPException as exc:
            await admin_service.audit(
                session_hash, "upload_lyric", 1, source, "failed", str(exc.detail), request,
            )
            raise
        await invalidate_media_catalog()
        return {"path": saved_path}
    finally:
        await file.close()


@router.get("/lyrics/catalog")
async def lyric_catalog(
    request: Request,
    track_path: str = Query("music", max_length=1024),
    lyric_path: str = Query("lyrics", max_length=1024),
    track_q: str = Query("", max_length=100),
    lyric_q: str = Query("", max_length=100),
    session_hash: str = Depends(require_session),
):
    try:
        result = await lyrics.catalog(track_path, lyric_path, track_q, lyric_q)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


@router.post("/lyrics/relations")
async def lyric_relations(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    linked_paths = payload.get("linked_paths")
    if (
        not isinstance(linked_paths, list)
        or len(linked_paths) > settings.ADMIN_MAX_BATCH_FILES
        or any(not isinstance(path, str) for path in linked_paths)
    ):
        raise HTTPException(status_code=400, detail="关联目标无效")
    try:
        count = await lyrics.replace_relations(
            str(payload.get("origin_kind", "")),
            str(payload.get("origin_path", "")),
            linked_paths,
            audit=_mutation_audit(session_hash, "lyric_relations", linked_paths, request),
        )
    except ValueError as exc:
        await admin_service.audit(session_hash, "lyric_relations", len(linked_paths), str(payload.get("origin_path", "")), "failed", str(exc), request)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "relations": count}


@router.post("/key/rotate")
async def admin_key_rotate(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    if request.scope.get("admin_credential_kind") != "persistent":
        raise HTTPException(status_code=403, detail="临时 Admin Key 会话不能修改长期 Admin Key")
    mode = payload.get("mode")
    if mode not in {"random", "custom"}:
        raise HTTPException(status_code=400, detail="Admin Key 生成模式无效")
    try:
        async with engine.begin() as conn:
            await admin_service.audit(session_hash, "admin_key_rotate", 1, mode, "pending", "", request, conn=conn)
        new_key = await admin_service.rotate_admin_key(
            session_hash,
            None if mode == "random" else str(payload.get("key", "")),
            None if mode == "random" else str(payload.get("confirmation", "")),
        )
    except ValueError as exc:
        await admin_service.audit(session_hash, "admin_key_rotate", 1, mode, "failed", str(exc), request)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await admin_service.audit(session_hash, "admin_key_rotate", 1, mode, "success", "", request)
    return JSONResponse({"status": "ok", "admin_key": new_key}, headers={"Cache-Control": "private, no-store"})


@router.post("/key/temporary")
async def admin_temporary_key(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    if request.scope.get("admin_credential_kind") != "persistent":
        raise HTTPException(status_code=403, detail="临时 Admin Key 会话不能继续签发临时 Key")
    try:
        minutes = int(payload.get("minutes", 15))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="临时 Admin Key 有效期无效") from exc
    try:
        temporary_key = await admin_service.issue_temporary_admin_key(session_hash, minutes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await admin_service.audit(
        session_hash,
        "temporary_admin_key_issue",
        1,
        f"{minutes}m",
        "success",
        "single_use=true",
        request,
    )
    return JSONResponse(
        {
            "status": "ok",
            "admin_key": temporary_key,
            "minutes": minutes,
            "single_use": True,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/security/blocks")
async def security_blocks(
    request: Request,
    ip: str | None = Query(None, max_length=45),
    match_mode: str = Query("exact", pattern="^(exact|fuzzy)$"),
    status: str | None = Query(None, max_length=32),
    ip_order: str = Query("asc", pattern="^(asc|desc)$"),
    sort_order: str | None = Query(
        None,
        pattern="^(ip_asc|ip_desc|last_attack_desc|last_attack_asc)$",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=200),
    session_hash: str = Depends(require_session),
):
    if settings.ADMIN_COOKIE_SECURE and not secure_admin_transport(request):
        raise HTTPException(status_code=426, detail="已启用 TLS，安全控制台只允许通过 HTTPS 访问")
    try:
        result = await ip_security.list_security_summary(
            ip_filter=ip,
            status_filter=status,
            page=page,
            page_size=page_size,
            ip_order=ip_order,
            sort_order=sort_order,
            match_mode=match_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["legal_api_count"] = ip_security.legal_api_count(request.app)
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


@router.post("/security/unban")
async def security_unban(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    if settings.ADMIN_COOKIE_SECURE and not secure_admin_transport(request):
        raise HTTPException(status_code=426, detail="已启用 TLS，安全控制台只允许通过 HTTPS 访问")
    try:
        ip = await ip_security.unban_ip(str(payload.get("ip", "")), session_hash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="IP 地址无效") from exc
    await admin_service.audit(session_hash, "security_unban", 1, ip, "success", "", request)
    return {"status": "ok", "ip": ip}


@router.post("/security/reban")
async def security_reban(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    if settings.ADMIN_COOKIE_SECURE and not secure_admin_transport(request):
        raise HTTPException(status_code=426, detail="已启用 TLS，安全控制台只允许通过 HTTPS 访问")
    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        raise HTTPException(status_code=400, detail="封禁原因无效")
    try:
        result = await ip_security.manual_ban_ip(str(payload.get("ip", "")), session_hash, reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await admin_service.audit(
        session_hash,
        "security_reban",
        1,
        result["ip"],
        "success",
        reason,
        request,
    )
    return {"status": "ok", **result}


@router.post("/security/permanent-ban")
async def security_permanent_ban(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    if settings.ADMIN_COOKIE_SECURE and not secure_admin_transport(request):
        raise HTTPException(status_code=426, detail="已启用 TLS，安全控制台只允许通过 HTTPS 访问")
    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        raise HTTPException(status_code=400, detail="拉黑原因无效")
    try:
        result = await ip_security.manual_permanent_ban_ip(
            str(payload.get("ip", "")), session_hash, reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await admin_service.audit(
        session_hash, "security_permanent_ban", 1, result["ip"], "success", reason, request,
    )
    return {"status": "ok", **result}


@router.post("/security/whitelist")
async def security_whitelist(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    if settings.ADMIN_COOKIE_SECURE and not secure_admin_transport(request):
        raise HTTPException(status_code=426, detail="已启用 TLS，安全控制台只允许通过 HTTPS 访问")
    note = payload.get("note", "")
    if not isinstance(note, str):
        raise HTTPException(status_code=400, detail="备注无效")
    try:
        ip = await ip_security.add_whitelist(str(payload.get("ip", "")), session_hash, note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="IP 地址无效") from exc
    await admin_service.audit(session_hash, "security_whitelist", 1, ip, "success", note, request)
    return {"status": "ok", "ip": ip}


@router.post("/security/whitelist/remove")
async def security_whitelist_remove(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    if settings.ADMIN_COOKIE_SECURE and not secure_admin_transport(request):
        raise HTTPException(status_code=426, detail="已启用 TLS，安全控制台只允许通过 HTTPS 访问")
    try:
        ip = await ip_security.remove_whitelist(str(payload.get("ip", "")), session_hash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="IP 地址无效") from exc
    await admin_service.audit(session_hash, "security_whitelist_remove", 1, ip, "success", "", request)
    return {"status": "ok", "ip": ip}


@router.get("/network/observations")
async def network_observations(
    request: Request,
    public_ip: str | None = Query(None, max_length=45),
    webrtc_ip: str | None = Query(None, max_length=45),
    match_mode: str = Query("exact", pattern="^(exact|fuzzy)$"),
    view: str = Query("pairs", pattern="^(pairs|public|webrtc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=200),
    session_hash: str = Depends(require_session),
):
    if settings.ADMIN_COOKIE_SECURE and not secure_admin_transport(request):
        raise HTTPException(status_code=426, detail="已启用 TLS，网络观测视图只允许通过 HTTPS 访问")
    try:
        if view == "pairs":
            result = await network_observation.list_observation_summary(
                public_ip=public_ip,
                webrtc_ip=webrtc_ip,
                page=page,
                page_size=page_size,
                match_mode=match_mode,
            )
        else:
            result = await network_observation.list_grouped_observation_summary(
                view,
                public_ip=public_ip,
                webrtc_ip=webrtc_ip,
                page=page,
                page_size=page_size,
                match_mode=match_mode,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


# ============================================================
# 6. Delete
# ============================================================

async def _delete_global_paths(paths: list[str], request: Request, session_hash: str) -> dict:
    from app.services.federation.state import state as node_state
    from app.services import resource_pool
    rows = await resource_pool.list_media(node_state.database)
    selected = [row for row in rows if any(row["media_path"] == path or row["media_path"].startswith(path.rstrip("/") + "/") for path in paths)]
    if not selected:
        raise HTTPException(404, "对象不存在")
    await resource_pool.mark_pending_delete([row["media_id"] for row in selected], node_state.database)
    deleted, pending = 0, []
    for row in selected:
        try:
            if row["storage_member_id"] == node_state.node["node_id"]:
                target = (MEDIA_ROOT / row["media_path"]).resolve()
                if MEDIA_ROOT not in target.parents:
                    raise p.ProtocolError("Invalid local placement")
                target.unlink(missing_ok=True)
            else:
                member, relation = await _member_and_relation_for_admin(row["storage_member_id"])
                if relation["status"] == "offline":
                    raise p.ProtocolError("Follower offline")
                token = p.storage_token(node_state.unseal(relation["credential"]), relation["relationship_id"],
                    node_state.node["node_id"], row["storage_member_id"], row["media_id"], row["object_id"],
                    "delete", row["media_path"], int(row["size_bytes"]), int(time.time()))
                async with httpx.AsyncClient(verify=ssl.create_default_context(), trust_env=False,
                                             timeout=httpx.Timeout(20, connect=8)) as client:
                    response = await client.post(relation["peer_endpoint"] + f"/internal/v1/storage/{row['object_id']}/delete",
                                                 headers={"X-Storage-Capability": token})
                if response.status_code != 200:
                    raise p.ProtocolError(f"Follower delete HTTP {response.status_code}")
            async with node_state.database.begin() as conn:
                await conn.execute(text("DELETE FROM media_lyric_links WHERE media_id=:id"), {"id": row["media_id"]})
                await conn.execute(text("DELETE FROM media_playback_events WHERE media_id=:id"), {"id": row["media_id"]})
                await conn.execute(text("DELETE FROM media_playback_stats WHERE media_id=:id"), {"id": row["media_id"]})
                if row["storage_member_id"] == node_state.node["node_id"]:
                    await conn.execute(text("DELETE FROM media_objects WHERE media_id=:id"), {"id": row["object_id"]})
            await resource_pool.complete_delete(row["media_id"], node_state.database)
            deleted += 1
        except Exception:
            pending.append(row["media_id"])
    await admin_service.audit(session_hash, "global-media-delete", len(selected),
                              json.dumps(paths, ensure_ascii=False), "success" if not pending else "pending",
                              json.dumps({"deleted": deleted, "pending_delete": pending}), request)
    await invalidate_media_catalog()
    return {"deleted": deleted, "pending_delete": pending}


async def _member_and_relation_for_admin(member_id: str) -> tuple[dict, dict]:
    from app.services.federation.state import state as node_state
    from app.services import resource_pool
    member = next((item for item in await resource_pool.list_members(node_state.database)
                   if item["member_id"] == member_id), None)
    if not member or not member["relationship_id"]:
        raise p.ProtocolError("Storage member unavailable")
    return member, await node_state.relationship(member["relationship_id"])

@router.post("/delete")
async def delete_objects(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    paths = payload.get("paths")

    if (
        not isinstance(paths, list)
        or not paths
        or len(paths)
        > settings.ADMIN_MAX_BATCH_FILES
    ):
        raise HTTPException(
            status_code=400,
            detail="请选择合法对象",
        )

    try:
        from app.services.federation.state import state as node_state
        if node_state.node["role"] == "Master" and all(str(path).split("/", 1)[0] in {"music", "vido"} for path in paths):
            return await _delete_global_paths(paths, request, session_hash)
        count = await MediaManager.delete(paths, audit=_mutation_audit(session_hash, "delete", paths, request))
    except HTTPException as exc:
        await _mutation_audit(session_hash, "delete", paths, request)(None, "failed", len(paths), {"reason": str(exc.detail)})
        raise
    await invalidate_media_catalog()


    return {
        "deleted": count,
    }


# ============================================================
# 7. Hide or restore
# ============================================================

@router.post("/hide")
async def hide_objects(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    paths = payload.get("paths")
    hidden = payload.get(
        "hidden",
        True,
    )

    if (
        not isinstance(paths, list)
        or not paths
        or len(paths) > settings.ADMIN_MAX_BATCH_FILES
        or not isinstance(hidden, bool)
    ):
        raise HTTPException(
            status_code=400,
            detail="隐藏参数无效",
        )

    action = "hide" if hidden else "unhide"
    try:
        await MediaManager.set_hidden(paths, hidden, audit=_mutation_audit(session_hash, action, paths, request))
    except HTTPException as exc:
        await _mutation_audit(session_hash, action, paths, request)(None, "failed", len(paths), {"reason": str(exc.detail)})
        raise
    await invalidate_media_catalog()


    return {
        "status": "ok",
        "hidden": hidden,
    }


# ============================================================
# 8. Download
# ============================================================

@router.get("/download")
async def download_objects(
    request: Request,
    paths: str,
    session_hash: str = Depends(require_session),
):
    try:
        items = json.loads(paths)

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="下载参数无效",
        )

    if (
        not isinstance(items, list)
        or not items
        or len(items)
        > settings.ADMIN_MAX_DOWNLOAD_ITEMS
    ):
        raise HTTPException(
            status_code=400,
            detail="请选择合法下载对象",
        )

    objects = await MediaManager._collect(
        items,
    )

    if (
        len(objects) == 1
        and objects[0][1].is_file()
    ):
        rel, path = objects[0]

        await admin_service.audit(
            session_hash,
            "download",
            1,
            rel,
            "success",
            "single_file",
            request,
        )

        # Uploads created before the Nginx download path was introduced may
        # still be 0600. Repair those lazily so existing media does not fail
        # with a permission-denied 403 after the internal redirect.
        MediaManager.ensure_download_readable(path)

        # Authentication stays in FastAPI, while Nginx sends the validated file
        # with sendfile. The internal location cannot be requested directly.
        return Response(
            headers={
                "X-Accel-Redirect": f"/_protected_media/{quote(rel, safe='/')}",
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''"
                    f"{quote(path.name, safe='')}"
                ),
                "Cache-Control": "private, no-store",
            },
        )

    archive = await MediaManager.build_zip_stream(
        items,
    )

    await admin_service.audit(
        session_hash,
        "download",
        len(items),
        json.dumps(
            items,
            ensure_ascii=False,
        ),
        "success",
        "zip",
        request,
    )

    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="media-download.zip"',
            "Cache-Control": "private, no-store",
            # Do not let Nginx turn the response stream into another disk-backed
            # temporary archive when proxy buffering is enabled globally.
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================
# 9. Admin logout
# ============================================================

@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session_hash: str = Depends(require_session),
):
    await admin_service.logout_admin(
        request,
        response,
    )

    return {
        "status": "logged_out",
    }
