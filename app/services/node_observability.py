"""Operator-facing connection, storage and backup observability."""
from __future__ import annotations

import time

from app.services import resource_pool
from app.services.federation.state import state

BACKUP_INTERVAL_SECONDS = 24 * 60 * 60


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
    raw_state = str(observed.get("state") or configured.get("state") or "disabled")
    lag = max(0, now - last_success) if enabled and last_success else 0
    last_attempt = max(0, int(observed.get("last_attempt") or 0))
    last_attempt_state = str(observed.get("last_attempt_state") or raw_state)
    if not enabled:
        health = "disabled"
    elif raw_state == "receiving" or last_attempt_state == "receiving":
        health = "running"
    elif last_attempt_state == "failed":
        health = "failed"
    elif not last_success:
        health = "waiting-first-backup"
    elif lag > BACKUP_INTERVAL_SECONDS + 6 * 60 * 60:
        health = "stale"
    else:
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
        "last_size_bytes": max(0, int(observed.get("last_size_bytes") or 0)),
        "recovery_points": max(0, int(observed.get("recovery_points") or 0)),
        "last_attempt": last_attempt,
        "last_attempt_state": last_attempt_state,
    }


async def snapshot() -> dict:
    now = int(time.time())
    relationships = await state.list_relationships()
    relation_by_peer = {row["peer_id"]: row for row in relationships}
    result = {"generated_at": now, "role": state.node["role"], "members": []}
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
        observed_backup = summary.get("backup") if isinstance(summary.get("backup"), dict) else {}
        online = member["member_kind"] == "MasterLocal" or bool(relation and relation.get("status") == "online")
        desired_storage = {
            "enabled": bool(member.get("storage_enabled")),
            "allocated_bytes": int(member.get("allocated_bytes") or 0),
        }
        backup = member.get("backup") if isinstance(member.get("backup"), dict) else {}
        desired_backup = {"enabled": bool(backup.get("enabled"))}
        result["members"].append({
            "member_id": member["member_id"],
            "member_kind": member["member_kind"],
            "relationship_id": member.get("relationship_id"),
            "connection": _connection(relation),
            "sync": {
                "storage": "effective" if member["member_kind"] == "MasterLocal" else _resource_sync(
                    desired_storage, observed_storage, ("enabled", "allocated_bytes"), online),
                "backup": "effective" if member["member_kind"] == "MasterLocal" else _resource_sync(
                    desired_backup, observed_backup, ("enabled",), online),
            },
            "observed": {"storage": observed_storage, "backup": observed_backup},
            "backup": _backup_view(member, observed_backup, now),
        })
    return result
