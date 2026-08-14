import os
from fastapi import APIRouter, Request, Header, HTTPException, Query
from fastapi.responses import FileResponse
from app.services.wall import wall_service
from app.core.config import settings

router = APIRouter()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
STATIC_PATH = os.path.join(BASE_DIR)

@router.get("", include_in_schema=False)
async def read_wall_index():
    return FileResponse(os.path.join(STATIC_PATH, "static","wall", "index.html"))

def get_ip(req: Request):
    return req.headers.get("X-Real-IP") or req.client.host

@router.get("/list")
async def list_posts():
    return await wall_service.get_all()

@router.post("/publish")
async def create_post(request: Request, content: str = Query(...), x_token: str = Header(None)):
    is_admin = (x_token == settings.WALL_ADMIN_TOKEN)
    if not await wall_service.can_perform_action(request, is_admin):
        raise HTTPException(status_code=429, detail="操作太频繁，请冷却 4 分钟")
    return await wall_service.add_post(content, get_ip(request))

@router.post("/comment/{post_id}")
async def create_comment(post_id: str, request: Request, content: str = Query(...), x_token: str = Header(None)):
    is_admin = (x_token == settings.WALL_ADMIN_TOKEN)
    if not await wall_service.can_perform_action(request, is_admin):
        raise HTTPException(status_code=429, detail="冷却中")
    res = await wall_service.add_comment(post_id, content, get_ip(request))
    if not res:
        raise HTTPException(status_code=404, detail="该帖已焚毁")
    return res

@router.delete("/delete/{post_id}")
async def delete_post(post_id: str, x_token: str = Header(None)):
    if x_token != settings.WALL_ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="无权操作")
    await wall_service.delete_post(post_id)
    return {"status": "deleted"}