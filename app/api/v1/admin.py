import json
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
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.db import engine
from app.services import admin_service
from app.services import ip_security
from app.services import lyrics
from app.services import network_observation
from app.services import media_search, playback
from app.services.federation import routing as node_routing
from app.services.federation.catalog import catalog as node_catalog
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import MEDIA_ROOT, MediaManager


router = APIRouter()


class MediaPriorityChange(BaseModel):
    media_path: str = Field(min_length=1, max_length=1024)
    resource_id: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    delta: int


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

    return HTMLResponse(
        content=path.read_text(
            encoding="utf-8",
        ),
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
    return {
        "status": "ok",
        "session": True,
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
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
    session_hash: str = Depends(require_session),
):
    local = await playback.attach_stats_and_sort(
        await MediaManager.list_media_objects(), "admin-media-priority"
    )
    remote = [node_routing.item(row) for row in await node_catalog.resources()]
    remote = await node_routing.attach_master_stats(remote)
    items = local + remote
    normalized_query = media_search.normalized_query(q) if q else ""
    if normalized_query:
        items = [item for item in items if media_search.matches_search(
            media_search.build_search_text(item["title"], item["media_path"]),
            normalized_query,
        )]
    if media_type:
        items = [item for item in items if item["type"] == media_type]
    items.sort(key=lambda item: (
        -int(item.get("preference", 0)),
        int(item.get("play_score", 0)),
        str(item["media_path"]).casefold(),
        str(item["media_id"]),
    ))
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size],
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
                payload.resource_id, payload.media_path, delta=payload.delta, audit=audit,
            )
        return await playback.change_preference(
            MEDIA_ROOT, payload.media_path, payload.delta, audit=audit,
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
    return await MediaManager.list_tree(path)


@router.get("/tree/search")
async def admin_tree_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=100),
    path: str = Query("", max_length=1024),
    session_hash: str = Depends(require_session),
):
    try:
        return await MediaManager.search_tree(q, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ============================================================
# 5. Single-file upload; browsers submit multi-file and folder jobs one file
# at a time so progress remains accurate.
# ============================================================

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
        await admin_service.audit(session_hash, "lyric_relations", len(linked_paths), str(payload.get("origin_path", "")), "failed", str(exc), request)
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
