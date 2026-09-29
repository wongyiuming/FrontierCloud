"""Upload placement by user-visible site type and media-folder affinity.

The Admin UI selects a site type, never a concrete storage member. Existing
storage metadata remains canonical: site type is derived from member_kind and
transport, so historical media needs no migration.

A media folder is the smallest placement-affinity unit. The first upload into an
empty folder uses normal least-pressure placement within the selected site type.
After that reservation exists, every direct child media object in that same
folder must use the same storage member. A nested child folder is an independent
affinity unit and is free to select another ready member of the same site type.
"""
from __future__ import annotations

import time

from sqlalchemy import select

from app.services import resource_pool
from app.services.federation import protocol as p
from app.services.federation import schema as s


SITE_PRIMARY = "primary"
SITE_DIRECT = "direct"
SITE_RELAY = "relay"
SITE_TYPES = frozenset({SITE_PRIMARY, SITE_DIRECT, SITE_RELAY})
SITE_LABELS = {
    SITE_PRIMARY: "主站",
    SITE_DIRECT: "直连站点",
    SITE_RELAY: "中继站点",
}


def normalize_site_type(value: str | None) -> str:
    site_type = str(value or "").strip().lower()
    if site_type not in SITE_TYPES:
        raise p.ProtocolError("必须选择上传站点类型")
    return site_type


def site_type_for_member(member: dict) -> str | None:
    if str(member.get("member_kind") or "") == "MasterLocal":
        return SITE_PRIMARY
    transport = str(member.get("transport") or "")
    if transport == "Direct":
        return SITE_DIRECT
    if transport == "Relay":
        return SITE_RELAY
    return None


def site_type_for_transport(transport: str | None, *, local_default: bool = True) -> str | None:
    value = str(transport or "")
    if value == "Direct":
        return SITE_DIRECT
    if value == "Relay":
        return SITE_RELAY
    if value == "Local" or (not value and local_default):
        return SITE_PRIMARY
    return None


def media_folder_path(media_path: str) -> str:
    normalized = str(media_path or "").strip().strip("/")
    if "/" not in normalized:
        raise p.ProtocolError("媒体路径缺少所属文件夹")
    return normalized.rsplit("/", 1)[0]


def _folder_member_ids(folder_path: str, rows) -> set[str]:
    """Return owners for direct child files only; nested folders are independent."""
    folder = str(folder_path or "").strip().strip("/")
    result: set[str] = set()
    for row in rows:
        path = str(row["media_path"] or "").strip().strip("/")
        if not path or media_folder_path(path) != folder:
            continue
        member_id = str(row["storage_member_id"] or "").strip()
        if member_id:
            result.add(member_id)
    return result


async def folder_affinity_member(folder_path: str, database) -> str | None:
    """Resolve a folder's owner from durable media plus live upload reservations.

    No separate folder-placement table is needed. Active/pending-delete media
    preserve affinity until their catalog row is removed. A live reservation
    pins an otherwise-empty folder early enough to make concurrent uploads safe.
    """
    folder = str(folder_path or "").strip().strip("/")
    prefix = folder + "/"
    now = int(time.time())
    async with database.connect() as conn:
        durable = (await conn.execute(select(
            s.global_media.c.storage_member_id,
            s.global_media.c.media_path,
        ).where(
            s.global_media.c.state.in_(("active", "pending_delete")),
            s.global_media.c.media_path.startswith(prefix, autoescape=True),
        ))).mappings().all()
        reserved = (await conn.execute(select(
            s.upload_sessions.c.storage_member_id,
            s.upload_sessions.c.media_path,
        ).where(
            s.upload_sessions.c.state == "reserved",
            s.upload_sessions.c.expires_at > now,
            s.upload_sessions.c.media_path.startswith(prefix, autoescape=True),
        ))).mappings().all()

    owners = _folder_member_ids(folder, [*durable, *reserved])
    if len(owners) > 1:
        raise p.ProtocolError("该媒体文件夹的历史资源已分散在多个存储节点，禁止继续上传")
    return next(iter(owners), None)


def ready_for_upload(member: dict, size: int = 0) -> bool:
    required = max(1, int(size))
    return bool(
        member.get("storage_enabled")
        and str(member.get("health") or "") == "online"
        and member.get("writable")
        and int(member.get("available_bytes") or 0) >= required
    )


def placement_key(member: dict) -> tuple[float, int, str]:
    """Prefer the least occupied member, then more writable bytes.

    The key is used only for an empty affinity folder. Once a folder has an
    owner, later direct-child uploads stay on that owner and do not rebalance.
    """
    allocated = max(1, int(member.get("allocated_bytes") or 0))
    occupied = max(0, int(member.get("used_bytes") or 0) + int(member.get("reserved_bytes") or 0))
    pressure = occupied / allocated
    return pressure, -int(member.get("available_bytes") or 0), str(member.get("member_id") or "")


async def ready_members(site_type: str, size: int, database) -> list[dict]:
    site_type = normalize_site_type(site_type)
    members = [
        member for member in await resource_pool.list_members(database)
        if site_type_for_member(member) == site_type and ready_for_upload(member, size)
    ]
    return sorted(members, key=placement_key)


async def choose_member(site_type: str, size: int, database, *, folder_path: str | None = None) -> dict:
    site_type = normalize_site_type(site_type)
    members = await resource_pool.list_members(database)

    if folder_path:
        affinity_id = await folder_affinity_member(folder_path, database)
        if affinity_id:
            affinity = next((member for member in members if str(member.get("member_id")) == affinity_id), None)
            if affinity is None:
                raise p.ProtocolError("该媒体文件夹绑定的存储节点已不存在")
            actual_type = site_type_for_member(affinity)
            if actual_type != site_type:
                label = SITE_LABELS.get(actual_type or "", "其他站点")
                raise p.ProtocolError(f"该媒体文件夹已归属{label}，不能改用{SITE_LABELS[site_type]}上传")
            if not ready_for_upload(affinity, size):
                raise p.ProtocolError("该媒体文件夹绑定的存储节点当前不可写或容量不足")
            return affinity

    candidates = [
        member for member in members
        if site_type_for_member(member) == site_type and ready_for_upload(member, size)
    ]
    candidates.sort(key=placement_key)
    if not candidates:
        raise p.ProtocolError(f"没有 ready 且容量充足的{SITE_LABELS[site_type]}")
    return candidates[0]


def availability_summary(members: list[dict]) -> dict[str, dict]:
    result = {
        site_type: {"site_type": site_type, "label": label, "ready": 0, "available_bytes": 0}
        for site_type, label in SITE_LABELS.items()
    }
    for member in members:
        site_type = site_type_for_member(member)
        if site_type is None or not ready_for_upload(member):
            continue
        result[site_type]["ready"] += 1
        result[site_type]["available_bytes"] += int(member.get("available_bytes") or 0)
    return result
