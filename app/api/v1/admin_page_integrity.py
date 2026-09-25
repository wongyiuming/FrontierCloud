"""Admin shell with content-hashed integrity client assets."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api.v1 import admin as legacy_admin
from app.core.static_assets import static_asset_url


router = APIRouter()


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def admin_page(request: Request,
                     session_hash: str = Depends(legacy_admin.require_session)):
    path = Path(__file__).resolve().parents[3] / "static" / "media" / "admin.html"
    if not path.exists():
        raise HTTPException(500, "Admin 页面文件不存在")
    content = path.read_text(encoding="utf-8")
    for marker, asset in {
        "{{ADMIN_CSS_URL}}": "css/admin.css",
        "{{ADMIN_JS_URL}}": "js/admin.js",
        "{{NODES_JS_URL}}": "js/nodes.js",
    }.items():
        content = content.replace(marker, static_asset_url(asset))
    integrity = static_asset_url("js/admin-upload-integrity.js")
    brand_admin = static_asset_url("js/brand-admin.js")
    content = content.replace(
        "</body>",
        f'<script src="{brand_admin}"></script>\n<script src="{integrity}"></script>\n</body>',
    )
    return HTMLResponse(content=content, headers={"Cache-Control": "no-store"})
