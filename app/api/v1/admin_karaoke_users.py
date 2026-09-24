from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update

from app.api.v1.admin import require_session
from app.services import karaoke_accounts as accounts
from app.services import karaoke_schema as ks
from app.services.federation.runtime import runtime
from app.services.federation.state import state
from app.services.federation import schema as fs

router = APIRouter(prefix="/users")


class UserAction(BaseModel):
    action: str = Field(pattern="^(ban|unban|delete|quota)$")
    quota_mib: int | None = Field(None, ge=1, le=10 * 1024 * 1024)


@router.get("")
async def list_users(q: str = "", page: int = 1, page_size: int = 50,
                     _actor: str = Depends(require_session)):
    accounts.require_master()
    page, page_size = max(1, page), max(1, min(100, page_size))
    clauses = []
    params = {}
    query = select(ks.users)
    count = select(func.count()).select_from(ks.users)
    value = q.strip()
    if value:
        if len(value) < 2:
            raise HTTPException(400, "用户查询至少输入 2 个字符")
        pattern = f"%{value[:64]}%"
        query = query.where(ks.users.c.username.like(pattern))
        count = count.where(ks.users.c.username.like(pattern))
    async with state.database.connect() as conn:
        total = int(await conn.scalar(count) or 0)
        rows = (await conn.execute(query.order_by(ks.users.c.created_at.desc())
                                   .limit(page_size).offset((page - 1) * page_size))).mappings().all()
    return {"items": [accounts.public_user(dict(row)) | {"created_at": row["created_at"]} for row in rows],
            "pagination": {"page": page, "pages": max(1, (total + page_size - 1) // page_size), "total": total}}


async def _remove_files(user: dict) -> list[dict]:
    from app.api.v1.karaoke_users import _remove_recording_bytes
    async with state.database.connect() as conn:
        recordings = [dict(row) for row in (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.user_id == user["user_id"]))).mappings()]
    for recording in recordings:
        await _remove_recording_bytes(recording)
    return recordings


@router.post("/{user_id}")
async def mutate_user(user_id: str, payload: UserAction, request: Request,
                      actor: str = Depends(require_session)):
    accounts.require_master()
    async with state.database.connect() as conn:
        user = (await conn.execute(select(ks.users).where(ks.users.c.user_id == user_id))).mappings().first()
    if not user:
        raise HTTPException(404, "用户不存在")
    user = dict(user)
    if payload.action == "delete":
        recordings = await _remove_files(user)
        async with state.database.begin() as conn:
            locked = (await conn.execute(select(ks.users).where(
                ks.users.c.user_id == user_id
            ).with_for_update())).mappings().first()
            if not locked:
                return {"status": "deleted"}
            await accounts.audit(request, user_id, "admin-user-delete", "success", detail={"actor": actor}, conn=conn)
            await conn.execute(delete(ks.recordings).where(ks.recordings.c.user_id == user_id))
            await conn.execute(delete(ks.users).where(ks.users.c.user_id == user_id))
            by_member: dict[str, int] = {}
            for recording in recordings:
                by_member[recording["storage_member_id"]] = by_member.get(recording["storage_member_id"], 0) + int(recording["size_bytes"])
            for member_id, removed in by_member.items():
                await conn.execute(update(fs.storage_members).where(
                    fs.storage_members.c.member_id == member_id
                ).values(used_bytes=func.greatest(0, fs.storage_members.c.used_bytes - removed),
                         reserved_bytes=func.greatest(0, fs.storage_members.c.reserved_bytes - removed)))
        await accounts.invalidate_user_sessions(user_id)
        return {"status": "deleted"}
    values = {"updated_at": int(time.time())}
    if payload.action in {"ban", "unban"}:
        values["status"] = "banned" if payload.action == "ban" else "active"
    else:
        quota = int(payload.quota_mib or 0) * 1024 * 1024
        if quota < int(user["used_bytes"]):
            raise HTTPException(409, "新空间不能小于用户已使用空间")
        values["quota_bytes"] = quota
    async with state.database.begin() as conn:
        await conn.execute(update(ks.users).where(ks.users.c.user_id == user_id).values(**values))
        await accounts.audit(request, user_id, "admin-user-" + payload.action, "success",
                             detail={"actor": actor, **values}, conn=conn)
    if payload.action == "ban":
        await accounts.invalidate_user_sessions(user_id)
    return {"status": "ok"}
