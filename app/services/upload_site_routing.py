"""Upload placement by user-visible site type.

The Admin UI selects a site type, never a concrete storage member. Existing
storage metadata remains canonical: site type is derived from member_kind and
transport, so historical media needs no migration.
"""
from __future__ import annotations

from app.services import resource_pool
from app.services.federation import protocol as p


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

    Sequential uploads therefore spread across every ready member of the chosen
    type while naturally respecting members with different allocations.
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


async def choose_member(site_type: str, size: int, database) -> dict:
    members = await ready_members(site_type, size, database)
    if not members:
        raise p.ProtocolError(f"没有 ready 且容量充足的{SITE_LABELS[normalize_site_type(site_type)]}")
    return members[0]


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
