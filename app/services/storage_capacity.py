"""Human-facing storage capacity observations, separate from placement limits.

`resource_pool.available_bytes` remains the scheduler's writable ceiling. This
module exposes the four capacity facts operators actually need to inspect:
physical total, current physical free, configured allocation and project use.
"""
from __future__ import annotations

import shutil
import time


def local_physical_capacity() -> tuple[int, int]:
    from app.api.v1.media import MEDIA_ROOT

    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(MEDIA_ROOT)
    return int(usage.total), int(usage.free)


def enrich_local_resource_summary(summary: dict) -> dict:
    """Attach live host filesystem facts to a follower heartbeat summary."""
    storage = summary.setdefault("storage", {})
    total, free = local_physical_capacity()
    storage["physical_total_bytes"] = total
    storage["physical_free_bytes"] = free
    storage["project_used_bytes"] = int(storage.get("used_bytes") or 0)
    storage["current_allocated_bytes"] = int(storage.get("allocated_bytes") or 0)
    return summary


async def enrich_pool_summary(summary: dict, store) -> dict:
    """Replace stale display facts with the freshest physical observations available."""
    from sqlalchemy import update
    from app.services import resource_pool
    from app.services.federation import schema as s

    result = dict(summary)
    members = [dict(member) for member in summary.get("members", [])]
    relations = {
        relation["peer_id"]: relation
        for relation in await store.list_relationships()
        if relation.get("state") == "active"
    }
    local_total = local_free = None
    local_member_id = None
    reserve = int(resource_pool.PHYSICAL_RESERVE_BYTES)

    for member in members:
        kind = member.get("member_kind")
        if kind == "MasterLocal":
            if local_total is None:
                local_total, local_free = local_physical_capacity()
            local_member_id = member.get("member_id")
            member["physical_total_bytes"] = local_total
            member["physical_free_bytes"] = local_free
        elif kind == "Follower":
            relation = relations.get(member.get("member_id"))
            relation_summary = relation.get("summary") if relation else {}
            storage = relation_summary.get("storage") if isinstance(relation_summary, dict) else {}
            storage = storage if isinstance(storage, dict) else {}
            member["physical_total_bytes"] = max(0, int(storage.get("physical_total_bytes") or 0))
            if "physical_free_bytes" in storage:
                member["physical_free_bytes"] = max(0, int(storage.get("physical_free_bytes") or 0))
        else:
            member.setdefault("physical_total_bytes", 0)

        member["current_allocated_bytes"] = int(member.get("allocated_bytes") or 0)
        member["project_used_bytes"] = int(member.get("used_bytes") or 0)

        if kind != "Auto":
            logical = max(0, int(member.get("allocated_bytes") or 0)
                          - int(member.get("used_bytes") or 0)
                          - int(member.get("reserved_bytes") or 0))
            physical = max(0, int(member.get("physical_free_bytes") or 0) - reserve)
            available = min(logical, physical) if (
                member.get("health") == "online" and member.get("writable")
            ) else 0
            member["available_bytes"] = available
            member["online_writable_bytes"] = available

    # Keep the scheduler's MasterLocal pre-filter snapshot aligned with the live
    # observation. The final reservation path still performs its own live disk
    # check, so this only prevents a stale low/high snapshot from skewing candidates.
    if local_member_id and local_free is not None:
        async with store.database.begin() as conn:
            await conn.execute(update(s.storage_members).where(
                s.storage_members.c.member_id == local_member_id,
            ).values(physical_free_bytes=int(local_free), updated_at=int(time.time())))

    real_members = [member for member in members if member.get("member_kind") != "Auto"]
    auto_available = max((int(member.get("available_bytes") or 0) for member in real_members), default=0)
    for member in members:
        if member.get("member_kind") == "Auto":
            member["available_bytes"] = auto_available
            member["online_writable_bytes"] = auto_available

    result["members"] = members
    result["physical_total_bytes"] = sum(int(member.get("physical_total_bytes") or 0) for member in real_members)
    result["physical_free_bytes"] = sum(int(member.get("physical_free_bytes") or 0) for member in real_members)
    result["current_allocated_bytes"] = sum(int(member.get("allocated_bytes") or 0)
                                            for member in real_members if member.get("storage_enabled"))
    result["project_used_bytes"] = sum(int(member.get("used_bytes") or 0) for member in real_members)
    result["available_bytes"] = sum(int(member.get("available_bytes") or 0) for member in real_members)
    result["online_writable_bytes"] = result["available_bytes"]
    return result


async def observed_pool(store) -> dict:
    from app.services import resource_pool

    return await enrich_pool_summary(await resource_pool.pool_summary(store.database), store)
