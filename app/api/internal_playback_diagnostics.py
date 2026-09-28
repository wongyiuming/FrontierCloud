"""Signed temporary Follower-to-Master relay for playback continuity diagnostics."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from app.api.internal_nodes import authenticated
from app.services import playback_continuity_diagnostics as diagnostics
from app.services.federation.state import state

router = APIRouter(prefix="/internal/v1/playback-continuity-diagnostics", include_in_schema=False)


@router.post("")
async def relay(request: Request):
    if not diagnostics.enabled():
        raise HTTPException(410, "Temporary playback diagnostics retired")
    relation = await authenticated(request)
    if state.node["role"] != "Master" or relation["direction"] != "downstream":
        raise HTTPException(403, "Only a Master accepts playback diagnostic relay")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid diagnostic report") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "Invalid diagnostic report")
    payload = diagnostics.normalize_report(value)
    if not payload.get("diagnostic_id") or not payload.get("stage"):
        raise HTTPException(400, "Diagnostic identity and stage are required")
    await diagnostics.store.add(
        payload,
        source_node=relation["peer_id"],
        delivery="follower-relay",
    )
    return {"status": "accepted"}
