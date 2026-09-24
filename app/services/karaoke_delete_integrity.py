"""Recoverable two-phase deletion for Karaoke recordings and accounts."""
from __future__ import annotations

import time

from sqlalchemy import delete, func, select, update

from app.services import karaoke_accounts as accounts
from app.services import karaoke_schema as ks
from app.services.federation import schema as fs
from app.services.federation.state import state


async def stage_recording(user_id: str, recording_id: str, *, pending: bool) -> dict | None:
    now = int(time.time())
    async with state.database.begin() as conn:
        row = (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.recording_id == recording_id,
            ks.recordings.c.user_id == user_id,
        ).with_for_update())).mappings().first()
        if not row:
            return None
        row = dict(row)
        finalized = bool(row.get("sha256"))
        if row["state"] != "deleting":
            if pending and row["state"] != "pending":
                raise ValueError("录音已完成，不能取消预留")
            if not pending and row["state"] != "ready":
                raise ValueError("录音不在可删除状态")
            await conn.execute(update(ks.recordings).where(
                ks.recordings.c.recording_id == recording_id
            ).values(state="deleting", updated_at=now))
        elif pending == finalized:
            raise ValueError("录音删除状态与请求不匹配")
        row["state"] = "deleting"
        return row


async def _physical_delete(row: dict) -> None:
    from app.api.v1.karaoke_users import _remove_recording_bytes
    await _remove_recording_bytes(row)


async def finalize_recording_delete(user_id: str, recording_id: str, *, request=None,
                                    action: str | None = None, detail: dict | None = None) -> bool:
    now = int(time.time())
    async with state.database.begin() as conn:
        row = (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.recording_id == recording_id,
            ks.recordings.c.user_id == user_id,
        ).with_for_update())).mappings().first()
        if not row:
            return True
        if row["state"] != "deleting":
            return False
        size = int(row["size_bytes"])
        await conn.execute(delete(ks.recordings).where(
            ks.recordings.c.recording_id == recording_id
        ))
        await conn.execute(update(ks.users).where(ks.users.c.user_id == user_id).values(
            used_bytes=func.greatest(0, ks.users.c.used_bytes - size), updated_at=now,
        ))
        member_update = (update(fs.storage_members).where(
            fs.storage_members.c.member_id == row["storage_member_id"]
        ).values(used_bytes=func.greatest(0, fs.storage_members.c.used_bytes - size))
                         if row.get("sha256") else
                         update(fs.storage_members).where(
            fs.storage_members.c.member_id == row["storage_member_id"]
        ).values(reserved_bytes=func.greatest(0, fs.storage_members.c.reserved_bytes - size)))
        await conn.execute(member_update)
        if request is not None and action:
            await accounts.audit(request, user_id, action, "success", detail={
                "recording_id": recording_id, **(detail or {}),
            }, conn=conn)
    return True


async def recover_recording(user_id: str, recording_id: str, *, request=None,
                            action: str | None = None, detail: dict | None = None) -> bool:
    async with state.database.connect() as conn:
        row = (await conn.execute(select(ks.recordings).where(
            ks.recordings.c.recording_id == recording_id,
            ks.recordings.c.user_id == user_id,
            ks.recordings.c.state == "deleting",
        ))).mappings().first()
    if not row:
        return True
    try:
        await _physical_delete(dict(row))
    except Exception:
        return False
    return await finalize_recording_delete(
        user_id, recording_id, request=request, action=action, detail=detail,
    )


async def recover_deleting_recordings(user_id: str | None = None, limit: int = 100) -> int:
    query = select(ks.recordings.c.user_id, ks.recordings.c.recording_id).where(
        ks.recordings.c.state == "deleting"
    )
    if user_id:
        query = query.where(ks.recordings.c.user_id == user_id)
    async with state.database.connect() as conn:
        rows = (await conn.execute(query.order_by(ks.recordings.c.updated_at).limit(limit))).all()
    recovered = 0
    for owner, recording_id in rows:
        recovered += int(await recover_recording(str(owner), str(recording_id)))
    return recovered


async def stage_user_deletion(user_id: str, *, request=None, action: str | None = None,
                              detail: dict | None = None) -> bool:
    now = int(time.time())
    async with state.database.begin() as conn:
        user = (await conn.execute(select(ks.users).where(
            ks.users.c.user_id == user_id
        ).with_for_update())).mappings().first()
        if not user:
            return False
        await conn.execute(update(ks.users).where(ks.users.c.user_id == user_id).values(
            status="deleting", updated_at=now,
        ))
        await conn.execute(update(ks.recordings).where(
            ks.recordings.c.user_id == user_id,
            ks.recordings.c.state.in_(("pending", "ready")),
        ).values(state="deleting", updated_at=now))
        if request is not None and action:
            await accounts.audit(request, user_id, action, "pending", detail=detail or {}, conn=conn)
    await accounts.invalidate_user_sessions(user_id)
    return True


async def finish_user_deletion(user_id: str, *, request=None, action: str | None = None,
                               detail: dict | None = None) -> bool:
    async with state.database.begin() as conn:
        user = (await conn.execute(select(ks.users).where(
            ks.users.c.user_id == user_id
        ).with_for_update())).mappings().first()
        if not user:
            return True
        if user["status"] != "deleting":
            return False
        remaining = int(await conn.scalar(select(func.count()).select_from(ks.recordings).where(
            ks.recordings.c.user_id == user_id
        )) or 0)
        if remaining:
            return False
        if request is not None and action:
            await accounts.audit(request, user_id, action, "success", detail=detail or {}, conn=conn)
        await conn.execute(delete(ks.users).where(ks.users.c.user_id == user_id))
    await accounts.invalidate_user_sessions(user_id)
    return True


async def recover_user_deletion(user_id: str, *, request=None, action: str | None = None,
                                detail: dict | None = None) -> bool:
    await recover_deleting_recordings(user_id, limit=500)
    return await finish_user_deletion(
        user_id, request=request, action=action, detail=detail,
    )


async def recover_deleting_users(limit: int = 20) -> int:
    async with state.database.connect() as conn:
        user_ids = list((await conn.execute(select(ks.users.c.user_id).where(
            ks.users.c.status == "deleting"
        ).order_by(ks.users.c.updated_at).limit(limit))).scalars())
    recovered = 0
    for user_id in user_ids:
        recovered += int(await recover_user_deletion(str(user_id)))
    return recovered
