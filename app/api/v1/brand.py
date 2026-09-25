"""Public and Admin endpoints for brand logo assets."""
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.services import admin_service, brand_assets


public_router = APIRouter()
admin_router = APIRouter()
upload_router = APIRouter()


def _logo(kind: str) -> brand_assets.BrandLogo:
    try:
        return brand_assets.effective_logo(kind)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="Logo 资源不可用") from exc


async def require_admin(request: Request) -> str:
    return await admin_service.require_admin(request)


@public_router.get("/logo/{kind}", include_in_schema=False)
async def public_logo(kind: str):
    logo = _logo(kind)
    return FileResponse(
        logo.path,
        media_type=logo.media_type,
        headers={"Cache-Control": "no-cache, max-age=0"},
    )


@admin_router.get("")
@admin_router.get("/")
async def logo_status(_session_hash: str = Depends(require_admin)):
    return {
        "items": [
            brand_assets.describe(kind)
            for kind in ("entertainment", "media", "music")
        ],
        "max_upload_bytes": brand_assets.MAX_LOGO_BYTES,
    }


@upload_router.post("/{kind}")
async def upload_logo(
    kind: str,
    request: Request,
    file: UploadFile = File(...),
    session_hash: str = Depends(require_admin),
):
    try:
        data = await file.read(brand_assets.MAX_LOGO_BYTES + 1)
        logo = brand_assets.store_custom(kind, data)
    except ValueError as exc:
        raise HTTPException(
            status_code=413 if "8 MiB" in str(exc) else 400,
            detail=str(exc),
        ) from exc
    await admin_service.audit(
        session_hash,
        "brand-logo-upload",
        1,
        kind,
        "success",
        f"{logo.media_type}:{len(data)}",
        request,
    )
    return brand_assets.describe(kind)


@admin_router.get("/{kind}/download")
async def download_logo(
    kind: str,
    request: Request,
    session_hash: str = Depends(require_admin),
):
    logo = _logo(kind)
    await admin_service.audit(
        session_hash,
        "brand-logo-download",
        1,
        kind,
        "success",
        logo.source,
        request,
    )
    return FileResponse(
        logo.path,
        media_type=logo.media_type,
        filename=brand_assets.download_filename(kind, logo.suffix),
        headers={"Cache-Control": "private, no-store"},
    )


@admin_router.delete("/{kind}")
async def delete_logo(
    kind: str,
    request: Request,
    session_hash: str = Depends(require_admin),
):
    try:
        changed = brand_assets.delete_custom(kind)
        item = brand_assets.describe(kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await admin_service.audit(
        session_hash,
        "brand-logo-delete",
        1,
        kind,
        "success",
        "restored-default" if changed else "already-default",
        request,
    )
    return item
