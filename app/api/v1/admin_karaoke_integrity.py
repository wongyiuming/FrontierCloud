"""Recoverable Admin mutations for Karaoke users."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.v1 import admin_karaoke_users as legacy
from app.api.v1.admin import require_session
from app.services import karaoke_delete_integrity as deletion


router = APIRouter(prefix="/users")


@router.get("")
async def list_users(q: str = "", page: int = 1, page_size: int = 50,
                     actor: str = Depends(require_session)):
    await deletion.recover_deleting_users()
    return await legacy.list_users(q, page, page_size, actor)


@router.post("/{user_id}")
async def mutate_user(user_id: str, payload: legacy.UserAction, request: Request,
                      actor: str = Depends(require_session)):
    if payload.action != "delete":
        return await legacy.mutate_user(user_id, payload, request, actor)
    staged = await deletion.stage_user_deletion(
        user_id, request=request, action="admin-user-delete", detail={"actor": actor},
    )
    if not staged:
        raise HTTPException(404, "用户不存在")
    complete = await deletion.recover_user_deletion(
        user_id, request=request, action="admin-user-delete", detail={"actor": actor},
    )
    return {"status": "deleted" if complete else "deleting"}
