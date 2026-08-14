import json
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

from app.core.config import settings
from app.services import admin_service
from app.services.media_manager import (
    MEDIA_ROOT,
    MediaManager,
    resolve_safe_path,
)


router = APIRouter()


def client_ip(request: Request) -> str:
    return request.headers.get("X-Real-IP") or (
        request.client.host
        if request.client
        else "127.0.0.1"
    )


async def require_session(request: Request) -> str:
    return await admin_service.require_admin(request)


# ============================================================
# 1. 临时 Token -> Admin Session
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
# 2. 生成新的临时 Admin Token
#
# 这个接口不是公共 UI 使用的。
# 宿主机管理员可以使用现有 WALL_ADMIN_TOKEN 调用。
# ============================================================

@router.post("/token/issue")
async def issue_token(
    request: Request,
):
    supplied = request.headers.get("X-Token")

    if (
        not supplied
        or supplied != settings.WALL_ADMIN_TOKEN
    ):
        raise HTTPException(
            status_code=403,
            detail="无权操作",
        )

    token = await admin_service.issue_admin_token()

    return {
        "status": "issued",
        "expires_in": settings.ADMIN_TOKEN_TTL,
        "token": token,
    }


# ============================================================
# 3. Admin 页面
#
# 注意：
# admin_router 已经被 endpoints.py 挂载到：
#
# /api/v1/media/admin
#
# 因此这里不能再写 /admin。
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
async def admin_page():
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
# 4. Admin Session 状态
# ============================================================

@router.get("/status")
async def admin_status(
    request: Request,
    session_hash: str = Depends(require_session),
):
    return {
        "status": "ok",
        "session": True,
    }


# ============================================================
# 5. Admin 文件树
# ============================================================

@router.get("/tree")
async def admin_tree(
    request: Request,
    path: str = "",
    session_hash: str = Depends(require_session),
):
    return await MediaManager.list_tree(path)


# ============================================================
# 6. 上传文件
# ============================================================

@router.post("/upload/files")
async def upload_files(
    request: Request,
    files: Annotated[
        list[UploadFile],
        File(...),
    ],
    target_dir: Annotated[
        str,
        Form(...),
    ],
    session_hash: str = Depends(require_session),
):
    if (
        len(files)
        > settings.ADMIN_MAX_BATCH_FILES
    ):
        raise HTTPException(
            status_code=413,
            detail="一次上传文件数量超过限制",
        )

    target = MediaManager.validate_destination_dir(
        target_dir,
    )

    results = []
    errors = []

    for upload in files:
        try:
            results.append(
                await MediaManager.upload_one(
                    upload,
                    target,
                )
            )
        except HTTPException as exc:
            errors.append(
                {
                    "name": upload.filename,
                    "error": exc.detail,
                }
            )

    await admin_service.audit(
        session_hash,
        "upload_files",
        len(files),
        target_dir,
        "partial" if errors else "success",
        json.dumps(
            {
                "ok": results,
                "errors": errors,
            },
            ensure_ascii=False,
        ),
        request,
    )

    return {
        "success": results,
        "failed": errors,
    }


# ============================================================
# 7. 上传文件夹
# ============================================================

@router.post("/upload/folder")
async def upload_folder(
    request: Request,
    files: Annotated[
        list[UploadFile],
        File(...),
    ],
    relative_paths: Annotated[
        str,
        Form(...),
    ],
    target_dir: Annotated[
        str,
        Form(...),
    ],
    session_hash: str = Depends(require_session),
):
    if (
        len(files)
        > settings.ADMIN_MAX_BATCH_FILES
    ):
        raise HTTPException(
            status_code=413,
            detail="一次上传文件数量超过限制",
        )

    try:
        rels = json.loads(relative_paths)

        if (
            not isinstance(rels, list)
            or len(rels) != len(files)
        ):
            raise ValueError

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="上传文件夹的目录结构数据无效",
        )

    try:
        base = (
            resolve_safe_path(
                MEDIA_ROOT,
                target_dir,
            )
            if target_dir
            else MEDIA_ROOT
        )

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="非法上传目录",
        )

    if (
        not base.exists()
        or not base.is_dir()
    ):
        raise HTTPException(
            status_code=404,
            detail="目标目录不存在",
        )

    results = []
    errors = []

    for upload, rel in zip(files, rels):
        try:
            rel = MediaManager.normalize_relative(
                str(rel)
            )

            parts = rel.split("/")

            name = MediaManager.validate_name(
                parts[-1]
            )

            nested = "/".join(parts[:-1])

            destination_dir = (
                base
                if not nested
                else base / nested
            )

            destination_dir = (
                destination_dir.resolve()
            )

            if (
                not destination_dir.is_relative_to(
                    MEDIA_ROOT
                )
                or destination_dir == MEDIA_ROOT
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "禁止直接向 "
                        "data/media 根目录上传媒体文件"
                    ),
                )

            destination_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            upload.filename = name

            results.append(
                await MediaManager.upload_one(
                    upload,
                    destination_dir,
                )
            )

        except HTTPException as exc:
            errors.append(
                {
                    "name": rel,
                    "error": exc.detail,
                }
            )

    await admin_service.audit(
        session_hash,
        "upload_folder",
        len(files),
        target_dir,
        "partial" if errors else "success",
        json.dumps(
            {
                "ok": results,
                "errors": errors,
            },
            ensure_ascii=False,
        ),
        request,
    )

    return {
        "success": results,
        "failed": errors,
    }


# ============================================================
# 8. 删除
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
# 9. 移动
# ============================================================

@router.post("/move")
async def move_objects(
    request: Request,
    payload: dict,
    session_hash: str = Depends(require_session),
):
    paths = payload.get("paths")
    destination = payload.get("destination")

    if (
        not isinstance(paths, list)
        or not paths
        or not isinstance(destination, str)
    ):
        raise HTTPException(
            status_code=400,
            detail="移动参数无效",
        )

    if (
        len(paths)
        > settings.ADMIN_MAX_BATCH_FILES
    ):
        raise HTTPException(
            status_code=413,
            detail="一次移动对象数量超过限制",
        )

    result = await MediaManager.move(
        paths,
        destination,
    )

    await admin_service.audit(
        session_hash,
        "move",
        len(paths),
        json.dumps(
            paths,
            ensure_ascii=False,
        ),
        "success",
        json.dumps(
            result,
            ensure_ascii=False,
        ),
        request,
    )

    return {
        "moved": result,
    }


# ============================================================
# 10. 隐藏 / 恢复
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
# 11. 下载
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

        return FileResponse(
            path,
            filename=path.name,
            headers={
                "Cache-Control": (
                    "private, no-store"
                ),
            },
        )

    archive = await MediaManager.build_zip(
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

    return FileResponse(
        archive,
        filename="media-download.zip",
        media_type="application/zip",
        headers={
            "Cache-Control": (
                "private, no-store"
            ),
        },
        background=BackgroundTask(
            archive.unlink,
            missing_ok=True,
        ),
    )


# ============================================================
# 12. Admin 退出
# ============================================================

@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
):
    await admin_service.logout_admin(
        request,
        response,
    )

    return {
        "status": "logged_out",
    }