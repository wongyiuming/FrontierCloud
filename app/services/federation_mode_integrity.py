"""Prevent Direct/Relay transport changes while uploads are reserved.

Upload placement and transport selection are one transaction boundary from the
operator's point of view. A member with a durable reserved upload therefore
cannot change transport until that reservation is completed or cancelled.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.services import resource_pool
from app.services.federation import protocol as p, schema as s
from app.services.federation.state import State


async def _reserved_upload_count(store: State, member_id: str) -> int:
    async with store.database.connect() as conn:
        value = await conn.scalar(
            select(func.count()).select_from(s.upload_sessions).where(
                s.upload_sessions.c.storage_member_id == member_id,
                s.upload_sessions.c.state == "reserved",
            )
        )
    return int(value or 0)


async def _set_mode_with_upload_fence(original, store: State, identifier: str,
                                      mode: str, actor: str):
    if mode not in ("Relay", "Direct"):
        return await original(store, identifier, mode, actor)

    relation = await store.relationship(identifier)
    if relation.get("mode") == mode:
        return await original(store, identifier, mode, actor)

    # Upload reservation takes the same lock while choosing a site-type member
    # and writing the durable reservation. Re-read under this lock so neither
    # operation can observe a stale transport boundary.
    async with resource_pool.storage_write_lock:
        relation = await store.relationship(identifier)
        if relation.get("mode") == mode:
            return await original(store, identifier, mode, actor)
        if await _reserved_upload_count(store, str(relation["peer_id"])):
            raise p.ProtocolError(
                "节点存在进行中的上传；完成或取消后才能切换 Direct/Relay"
            )
        return await original(store, identifier, mode, actor)


def install() -> None:
    if getattr(State, "_upload_mode_fence_installed", False):
        return
    original = State.set_mode

    async def set_mode(self: State, identifier: str, mode: str, actor: str):
        return await _set_mode_with_upload_fence(
            original, self, identifier, mode, actor,
        )

    State.set_mode = set_mode
    State._upload_mode_fence_installed = True
