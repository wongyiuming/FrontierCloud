import json
import secrets
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

from app.core.config import settings
from app.core.admin_log import admin_log_buffer
from app.services import admin_service
from app.services import ip_security
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import MediaManager


router = APIRouter()


async def require_session(request: Request) -> str:
    return await admin_service.require_admin(request)


def secure_admin_transport(request: Request) -> bool:
    # Direct loopback HTTP is allowed only for the local IDE workflow.
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    direct_scheme = request.url.scheme.lower()
    client_host = request.client.host if request.client else ""
    return forwarded_proto == "https" or direct_scheme == "https" or client_host in {"127.0.0.1", "::1"}


# ============================================================
# 1. Temporary token to admin session
# ============================================================

@router.post("/elevate")
async def elevate(
    request: Request,
    response: Response,
    token: str = Form(...),
):
    token_hash = await admin_service.verify_admin_token(
        token,
        request,
    )

    await admin_service.create_session(
        token_hash,
        request,
        response,
    )

    return {
        "status": "ok",
        "redirect": "/api/v1/media/admin",
    }


# ============================================================
# 2. Issue a temporary admin token
#
# This endpoint is reserved for host administrators and is not used by the
# public UI. Authenticate with ADMIN_BOOTSTRAP_TOKEN.
# ============================================================

@router.post("/token/issue")
async def issue_token(
    request: Request,
):
    supplied = request.headers.get("X-Token")

    if (
        not supplied
        or not secrets.compare_digest(supplied, settings.ADMIN_BOOTSTRAP_TOKEN)
    ):
        raise HTTPException(
            status_code=403,
            detail="无权操作",
        )

    token = await admin_service.issue_admin_token()

    return {
        "status": "issued",
        "expires_in": settings.ADMIN_TOKEN_TTL + admin_service.TOKEN_ISSUE_OVERLAP_SECONDS,
        "active_ttl": settings.ADMIN_TOKEN_TTL,
        "automatic_issue_interval": settings.ADMIN_TOKEN_ISSUE_INTERVAL,
        "token": token,
    }


# ============================================================
# 3. Admin page
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
# 4. Admin session status
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
        },
        "csrf_cookie_name": settings.ADMIN_CSRF_COOKIE_NAME,
    }


# ============================================================
# 5. Admin file tree
# ============================================================

@router.get("/tree")
async def admin_tree(
    request: Request,
    path: str = "",
    session_hash: str = Depends(require_session),
):
    return await MediaManager.list_tree(path)


# ============================================================
# 6. Single-file upload; browsers submit multi-file and folder jobs one file
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
    try:
        if relative_path:
            target, upload_name = MediaManager.folder_upload_target(target_dir, relative_path)
            file.filename = upload_name
            source = relative_path
        else:
            target = MediaManager.validate_destination_dir(target_dir)
            source = file.filename or ""

        try:
            saved_path = await MediaManager.upload_one(file, target)
        except HTTPException as exc:
            await admin_service.audit(session_hash, "upload_item", 1, source, "failed", str(exc.detail), request)
            raise

        await invalidate_media_catalog()
        await admin_service.audit(session_hash, "upload_item", 1, source, "success", saved_path, request)
        return {"path": saved_path}
    finally:
        await file.close()


@router.get("/logs")
async def admin_logs(
    request: Request,
    after: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    session_hash: str = Depends(require_session),
):
    if settings.ADMIN_COOKIE_SECURE and not secure_admin_transport(request):
        raise HTTPException(status_code=426, detail="生产环境日志控制台只允许通过 HTTPS 访问")
    return JSONResponse(
        {"entries": admin_log_buffer.read(after=after, limit=limit), "secure_transport": secure_admin_transport(request)},
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/security/blocks")
async def security_blocks(
    request: Request,
    ip: str | None = Query(None, max_length=45),
    status: str | None = Query(None, max_length=32),
    scope: str = Query("recent", pattern="^(recent|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=200),
    session_hash: str = Depends(require_session),
):
    if settings.ADMIN_COOKIE_SECURE and not secure_admin_transport(request):
        raise HTTPException(status_code=426, detail="生产环境安全控制台只允许通过 HTTPS 访问")
    try:
        result = await ip_security.list_security_history(
            ip_filter=ip,
            status_filter=status,
            page=page,
            page_size=page_size,
            recent_only=scope == "recent",
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
        raise HTTPException(status_code=426, detail="生产环境安全控制台只允许通过 HTTPS 访问")
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
        raise HTTPException(status_code=426, detail="生产环境安全控制台只允许通过 HTTPS 访问")
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


@router.post("/security/whitelist")
async def security_whitelist(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    if settings.ADMIN_COOKIE_SECURE and not secure_admin_transport(request):
        raise HTTPException(status_code=426, detail="生产环境安全控制台只允许通过 HTTPS 访问")
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
        raise HTTPException(status_code=426, detail="生产环境安全控制台只允许通过 HTTPS 访问")
    try:
        ip = await ip_security.remove_whitelist(str(payload.get("ip", "")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="IP 地址无效") from exc
    await admin_service.audit(session_hash, "security_whitelist_remove", 1, ip, "success", "", request)
    return {"status": "ok", "ip": ip}


# ============================================================
# 7. Delete
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

    count = await MediaManager.delete(
        paths,
    )
    await invalidate_media_catalog()

    await admin_service.audit(
        session_hash,
        "delete",
        len(paths),
        json.dumps(
            paths,
            ensure_ascii=False,
        ),
        "success",
        f"deleted={count}",
        request,
    )

    return {
        "deleted": count,
    }


# ============================================================
# 8. Hide or restore
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
        or not isinstance(hidden, bool)
    ):
        raise HTTPException(
            status_code=400,
            detail="隐藏参数无效",
        )

    await MediaManager.set_hidden(
        paths,
        hidden,
    )
    await invalidate_media_catalog()

    await admin_service.audit(
        session_hash,
        "hide" if hidden else "unhide",
        len(paths),
        json.dumps(
            paths,
            ensure_ascii=False,
        ),
        "success",
        "",
        request,
    )

    return {
        "status": "ok",
        "hidden": hidden,
    }


# ============================================================
# 9. Download
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
# 10. Admin logout
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
