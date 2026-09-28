"""Temporary playback-continuity diagnostics with signed Follower-to-Master relay."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.v1.admin import require_session
from app.services import playback_continuity_diagnostics as diagnostics
from app.services.federation.runtime import runtime
from app.services.federation.state import state

router = APIRouter(prefix="/playback-continuity-diagnostics")
admin_router = APIRouter(prefix="/playback-continuity-diagnostics")
MAX_BODY_BYTES = 24 * 1024
RELAY_PATH = "/internal/v1/playback-continuity-diagnostics"


async def _json_body(request: Request, *, authenticated_body: bool = False) -> dict:
    if authenticated_body:
        raw = request.state.node_control_body or b"{}"
    else:
        chunks: list[bytes] = []
        length = 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > MAX_BODY_BYTES:
                raise HTTPException(413, "Diagnostic report too large")
            chunks.append(chunk)
        raw = b"".join(chunks)
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, "Diagnostic report too large")
    try:
        value = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid diagnostic report") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "Invalid diagnostic report")
    return value


def _require_live_interface() -> None:
    if not diagnostics.enabled():
        raise HTTPException(410, "Temporary playback diagnostics retired")


@router.post("")
async def submit(request: Request):
    _require_live_interface()
    payload = diagnostics.normalize_report(await _json_body(request))
    if not payload.get("diagnostic_id") or not payload.get("stage"):
        raise HTTPException(400, "Diagnostic identity and stage are required")
    role = str(state.node.get("role") or "Standalone")
    node_id = str(state.node.get("node_id") or "")

    if role == "Follower":
        relations = await state.list_relationships()
        upstream = next((row for row in relations
                         if row["direction"] == "upstream" and row["state"] == "active"), None)
        if upstream is not None:
            try:
                await asyncio.wait_for(runtime.call(upstream, RELAY_PATH, payload), timeout=2.5)
                return Response(status_code=202, headers={"Cache-Control": "no-store"})
            except Exception:
                await diagnostics.store.add(
                    payload,
                    source_node=node_id,
                    delivery="follower-fallback",
                )
                return Response(status_code=202, headers={"Cache-Control": "no-store"})

    await diagnostics.store.add(
        payload,
        source_node=node_id,
        delivery="master-local" if role == "Master" else "standalone-local",
    )
    return Response(status_code=202, headers={"Cache-Control": "no-store"})


@admin_router.get("")
async def snapshot(_actor: str = Depends(require_session)):
    return await diagnostics.store.snapshot()


@admin_router.delete("")
async def clear(_actor: str = Depends(require_session)):
    await diagnostics.store.clear()
    return {"status": "cleared"}
