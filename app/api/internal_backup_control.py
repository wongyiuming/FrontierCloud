"""Authenticated cleanup for interrupted federation backup generations."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, select, update

from app.api.internal_nodes import authenticated
from app.services.federation import schema as s
from app.services.federation.state import state

router = APIRouter(prefix="/internal/v1/backup", include_in_schema=False)


def require_master_relation(relation: dict) -> None:
    if state.node.get("role") != "Follower" or relation.get("direction") != "upstream":
        raise HTTPException(403, "Only the paired Master can control Follower backups")


@router.post("/abort")
async def abort(request: Request):
    relation = await authenticated(request)
    require_master_relation(relation)
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        generation = int(value["generation"])
        if generation <= 0:
            raise ValueError()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid backup generation") from exc

    now = int(time.time())
    master_id = relation["peer_id"]
    async with state.database.begin() as conn:
        receiving = list((await conn.execute(select(s.business_backups.c.generation).where(
            s.business_backups.c.master_id == master_id,
            s.business_backups.c.state == "receiving",
        ).with_for_update())).scalars())
        if receiving:
            await conn.execute(delete(s.business_backup_chunks).where(
                s.business_backup_chunks.c.master_id == master_id,
                s.business_backup_chunks.c.generation.in_(receiving),
            ))
            await conn.execute(update(s.business_backups).where(
                s.business_backups.c.master_id == master_id,
                s.business_backups.c.generation.in_(receiving),
            ).values(state="failed", size_bytes=0, chunk_count=0, updated_at=now))
            await conn.execute(update(s.backup_members).where(
                s.backup_members.c.member_id == state.node["node_id"],
                s.backup_members.c.enabled == 1,
            ).values(state="failed", updated_at=now))
    return {"status": "failed" if receiving else "clean", "generation": generation,
            "aborted_generations": len(receiving)}
