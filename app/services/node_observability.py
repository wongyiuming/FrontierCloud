"""Operator-facing node observability derived from existing federation state.

This module intentionally adds no schema.  It turns the resource-pool and worker
facts already owned by the Master into an operations view: desired vs observed
configuration, real leased-job occupancy, recent worker activity, backup
freshness, and connection telemetry.
"""
from __future__ import annotations

import time

from sqlalchemy import func, select

from app.services import resource_pool
from app.services.federation import schema as s
from app.services.federation.state import state

DAY_SECONDS = 24 * 60 * 60
BACKUP_INTERVAL_SECONDS = DAY_SECONDS


def _resource_sync(desired: dict, observed: dict, keys: tuple[str, ...], online: bool) -> str:
    if not online:
        return "offline"
    if not observed:
        return "awaiting"
    for key in keys:
        left, right = desired.get(key), observed.get(key)
        if isinstance(left, bool):
            right = bool(right)
        if left != right:
            return "syncing"
    return "effective"


def _connection(relation: dict | None) -> dict:
    if not relation:
        return {
            "status": "local", "current_rtt_ms": 0, "min_rtt_ms": 0,
            "avg_rtt_ms": 0, "max_rtt_ms": 0, "sample_count": 0,
            "last_heartbeat": 0, "failures": 0, "recoveries": 0,
        }
    summary = relation.get("summary") if isinstance(relation.get("summary"), dict) else {}
    heartbeat = summary.get("heartbeat") if isinstance(summary.get("heartbeat"), dict) else {}
    current = max(0, int(heartbeat.get("current_ms") or relation.get("rtt_ms") or 0))
    return {
        "status": relation.get("status") or "unknown",
        "current_rtt_ms": current,
        "min_rtt_ms": max(0, int(heartbeat.get("min_ms") or current)),
        "avg_rtt_ms": max(0, int(heartbeat.get("avg_ms") or current)),
        "max_rtt_ms": max(0, int(heartbeat.get("max_ms") or current)),
        "sample_count": max(0, int(heartbeat.get("count") or (1 if current else 0))),
        "window_seconds": max(0, int(heartbeat.get("window_seconds") or 3600)),
        "last_heartbeat": max(0, int(relation.get("last_heartbeat") or 0)),
        "failures": max(0, int(relation.get("failures") or 0)),
        "recoveries": max(0, int(relation.get("recoveries") or 0)),
    }


def _backup_view(member: dict, observed: dict, now: int) -> dict:
    configured = member.get("backup") if isinstance(member.get("backup"), dict) else {}
    enabled = bool(configured.get("enabled"))
    last_success = max(0, int(configured.get("last_success") or observed.get("last_success") or 0))
    raw_state = str(configured.get("state") or observed.get("state") or "disabled")
    lag = max(0, now - last_success) if enabled and last_success else 0
    if not enabled:
        health = "disabled"
    elif raw_state == "receiving":
        health = "running"
    elif not last_success:
        health = "waiting-first-backup"
    elif lag > BACKUP_INTERVAL_SECONDS + 6 * 60 * 60:
        health = "stale"
    else:
        # A resource-configuration heartbeat historically rewrote ready to
        # pending.  Freshness is therefore based on the durable success stamp,
        # while raw_state remains visible in the technical detail.
        health = "healthy"
    return {
        "enabled": enabled,
        "health": health,
        "raw_state": raw_state,
        "generation": max(0, int(configured.get("generation") or observed.get("generation") or 0)),
        "last_success": last_success,
        "lag_seconds": lag,
        "next_due": last_success + BACKUP_INTERVAL_SECONDS if enabled and last_success else 0,
        "checksum": str(configured.get("checksum") or observed.get("checksum") or "")[:64],
    }


def _placement_reason(row: dict) -> str:
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    placement = result.get("_placement") if isinstance(result.get("_placement"), dict) else {}
    reason = str(placement.get("reason") or "")
    if reason == "pinned":
        return "显式指定此节点"
    if reason == "capability-fifo":
        return "能力匹配 + FIFO，共享任务由该节点先领取"
    if row.get("state") == "queued" and row.get("member_id"):
        return "显式等待此节点领取"
    if int(row.get("attempts") or 0) > 0:
        return "已由此节点领取；旧任务未持久化原始调度理由"
    return "尚未调度"


async def _worker_activity(member_id: str, capabilities: list[str], database, now: int) -> dict:
    async with database.connect() as conn:
        rows = [dict(row) for row in (await conn.execute(
            select(s.worker_jobs).where(s.worker_jobs.c.member_id == member_id)
            .order_by(s.worker_jobs.c.updated_at.desc()).limit(12)
        )).mappings()]
        running = int(await conn.scalar(select(func.count()).select_from(s.worker_jobs).where(
            s.worker_jobs.c.member_id == member_id,
            s.worker_jobs.c.state == "leased",
            s.worker_jobs.c.lease_expires_at > now,
        )) or 0)
        queued_pinned = int(await conn.scalar(select(func.count()).select_from(s.worker_jobs).where(
            s.worker_jobs.c.member_id == member_id,
            s.worker_jobs.c.state == "queued",
        )) or 0)
        completed_24h = int(await conn.scalar(select(func.count()).select_from(s.worker_jobs).where(
            s.worker_jobs.c.member_id == member_id,
            s.worker_jobs.c.state == "complete",
            s.worker_jobs.c.updated_at >= now - DAY_SECONDS,
        )) or 0)
        retry_24h = int(await conn.scalar(select(func.coalesce(func.sum(
            func.greatest(s.worker_jobs.c.attempts - 1, 0)), 0)).where(
            s.worker_jobs.c.member_id == member_id,
            s.worker_jobs.c.updated_at >= now - DAY_SECONDS,
        )) or 0)
        shared_queued = 0
        if capabilities:
            shared_queued = int(await conn.scalar(select(func.count()).select_from(s.worker_jobs).where(
                s.worker_jobs.c.member_id.is_(None),
                s.worker_jobs.c.state == "queued",
                s.worker_jobs.c.job_type.in_(capabilities),
            )) or 0)
    recent = [{
        "job_id": row["job_id"],
        "job_type": row["job_type"],
        "media_id": row.get("media_id"),
        "state": row["state"],
        "attempts": int(row.get("attempts") or 0),
        "created_at": int(row.get("created_at") or 0),
        "updated_at": int(row.get("updated_at") or 0),
        "lease_expires_at": int(row.get("lease_expires_at") or 0),
        "placement_reason": _placement_reason(row),
    } for row in rows]
    return {
        "running": running,
        "queued_pinned": queued_pinned,
        "shared_queued": shared_queued,
        "completed_24h": completed_24h,
        "retries_24h": retry_24h,
        "recent": recent,
    }


async def snapshot() -> dict:
    now = int(time.time())
    relationships = await state.list_relationships()
    relation_by_peer = {row["peer_id"]: row for row in relationships}
    result = {
        "generated_at": now,
        "role": state.node["role"],
        "members": [],
        "cluster": {"shared_queued": 0, "running": 0, "completed_24h": 0},
    }
    if state.node["role"] != "Master":
        result["relationships"] = [{
            "relationship_id": row["relationship_id"],
            "peer_id": row["peer_id"],
            "connection": _connection(row),
        } for row in relationships]
        return result

    members = await resource_pool.list_members(state.database)
    for member in members:
        relation = relation_by_peer.get(member["member_id"])
        summary = relation.get("summary") if relation and isinstance(relation.get("summary"), dict) else {}
        observed_storage = summary.get("storage") if isinstance(summary.get("storage"), dict) else {}
        observed_compute = summary.get("compute") if isinstance(summary.get("compute"), dict) else {}
        observed_backup = summary.get("backup") if isinstance(summary.get("backup"), dict) else {}
        online = member["member_kind"] == "MasterLocal" or bool(relation and relation.get("status") == "online")
        desired_storage = {
            "enabled": bool(member.get("storage_enabled")),
            "allocated_bytes": int(member.get("allocated_bytes") or 0),
        }
        compute = member.get("compute") if isinstance(member.get("compute"), dict) else {}
        desired_compute = {
            "enabled": bool(compute.get("enabled")),
            "worker_slots": int(compute.get("worker_slots") or 0),
        }
        backup = member.get("backup") if isinstance(member.get("backup"), dict) else {}
        desired_backup = {"enabled": bool(backup.get("enabled"))}
        capabilities = [str(item) for item in compute.get("capabilities", []) if isinstance(item, str)]
        activity = await _worker_activity(member["member_id"], capabilities, state.database, now)
        slots = desired_compute["worker_slots"] if desired_compute["enabled"] else 0
        compute_view = {
            **desired_compute,
            "cpu_percent": int(compute.get("cpu_percent") or observed_compute.get("cpu_percent") or 0),
            "memory_available_bytes": int(compute.get("memory_available_bytes") or observed_compute.get("memory_available_bytes") or 0),
            "capabilities": capabilities,
            "running": activity["running"],
            "available_slots": max(0, slots - activity["running"]),
            "queued_pinned": activity["queued_pinned"],
            "shared_queued": activity["shared_queued"],
            "completed_24h": activity["completed_24h"],
            "retries_24h": activity["retries_24h"],
            "recent": activity["recent"],
        }
        item = {
            "member_id": member["member_id"],
            "member_kind": member["member_kind"],
            "relationship_id": member.get("relationship_id"),
            "connection": _connection(relation),
            "sync": {
                "storage": "effective" if member["member_kind"] == "MasterLocal" else _resource_sync(
                    desired_storage, observed_storage, ("enabled", "allocated_bytes"), online),
                "compute": "effective" if member["member_kind"] == "MasterLocal" else _resource_sync(
                    desired_compute, observed_compute, ("enabled", "worker_slots"), online),
                "backup": "effective" if member["member_kind"] == "MasterLocal" else _resource_sync(
                    desired_backup, observed_backup, ("enabled",), online),
            },
            "observed": {
                "storage": observed_storage,
                "compute": observed_compute,
                "backup": observed_backup,
            },
            "compute": compute_view,
            "backup": _backup_view(member, observed_backup, now),
        }
        result["members"].append(item)
        result["cluster"]["running"] += activity["running"]
        result["cluster"]["completed_24h"] += activity["completed_24h"]
        result["cluster"]["shared_queued"] = max(result["cluster"]["shared_queued"], activity["shared_queued"])
    return result
