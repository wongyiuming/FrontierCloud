"""Authenticated Master-to-Follower release control over the existing TLS federation plane."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request

from app.api.internal_nodes import authenticated
from app.services.federation.state import state
from app.services.release_control import agent_request, agent_status

router = APIRouter(prefix="/internal/v1/cluster-update", include_in_schema=False)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def require_master_relation(relation: dict) -> None:
    if state.node.get("role") != "Follower" or relation.get("direction") != "upstream":
        raise HTTPException(403, "Only the paired Master can control Follower releases")


@router.post("/start")
async def start(request: Request):
    relation = await authenticated(request)
    require_master_relation(relation)
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        target = str(value["target_sha"])
        mode = str(value.get("mode") or "upgrade")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid release request") from exc
    if not SHA_RE.fullmatch(target) or mode not in {"upgrade", "rollback"}:
        raise HTTPException(400, "Invalid release target")
    response = await agent_request({
        "action": "start", "target_sha": target, "mode": mode, "hold_maintenance": False,
    })
    if not response.get("ok"):
        current = response.get("status") if isinstance(response.get("status"), dict) else {}
        if current.get("target_sha") == target and current.get("state") in {"queued", "running", "distributing", "success"}:
            return {"accepted": True, "status": current}
        raise HTTPException(409, str(response.get("reason") or "Follower updater rejected release"))
    return {"accepted": True, "target_sha": target, "mode": mode}


@router.post("/status")
async def status(request: Request):
    relation = await authenticated(request)
    require_master_relation(relation)
    return {"status": await agent_status()}
