"""Authenticated Master-to-Follower control over the existing TLS federation plane."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request

from app.api.internal_backup_control import router as backup_control_router
from app.api.internal_media_control import router as media_control_router
from app.api.internal_nodes import authenticated
from app.api.internal_playback_diagnostics import router as playback_diagnostics_router
from app.api.internal_worker_retirement import install as install_internal_worker_retirement
from app.services.federation.state import state
from app.services.federation import protocol as p
from app.services.federation import release_manifest as manifests
from app.services.release_control import agent_request, agent_status


install_internal_worker_retirement()

router = APIRouter(include_in_schema=False)
cluster_router = APIRouter(prefix="/internal/v1/cluster-update", include_in_schema=False)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def require_master_relation(relation: dict) -> None:
    if state.node.get("role") != "Follower" or relation.get("direction") != "upstream":
        raise HTTPException(403, "Only the paired Master can control Follower releases")


@cluster_router.post("/start")
async def start(request: Request):
    relation = await authenticated(request)
    require_master_relation(relation)
    try:
        raw = request.state.node_control_body or b"{}"
        if len(raw) > 16384:
            raise ValueError("release request exceeds limit")
        value = json.loads(raw, object_pairs_hook=manifests._pairs)
        if not isinstance(value, dict) or set(value) - {"target_sha", "mode", "release_manifest"}:
            raise ValueError("unknown release field")
        manifest = manifests.parse(p.canonical(value["release_manifest"])) if "release_manifest" in value else None
        target = str(value.get("target_sha") or "")
        mode = str(value.get("mode") or "upgrade")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid release request") from exc
    if (manifest is None and not SHA_RE.fullmatch(target) or manifest is not None and "target_sha" in value
            or mode not in {"upgrade", "rollback"}):
        raise HTTPException(400, "Invalid release target")
    payload = {
        "action": "start", "target_sha": target, "mode": mode, "hold_maintenance": False,
    }
    if manifest is not None:
        payload.pop("target_sha")
        payload["release_manifest"] = manifest
    response = await agent_request(payload)
    if manifest is not None:
        release_id = manifests.identifier(manifest)
        if response.get("ok"):
            if response.get("release_id") != release_id:
                raise HTTPException(409, "Follower updater lacks manifest acknowledgement")
            return {"accepted": True, "release_id": release_id, "mode": mode}
        current = response.get("status") if isinstance(response.get("status"), dict) else {}
        try:
            same = manifests.identifier(current.get("target_manifest")) == release_id
        except (ValueError, TypeError):
            same = False
        if same and current.get("mode") == mode and current.get("state") in {"queued", "running", "distributing", "restarting", "success"}:
            return {"accepted": True, "release_id": release_id, "status": current}
        raise HTTPException(409, "Follower updater rejected manifest")
    if not response.get("ok"):
        current = response.get("status") if isinstance(response.get("status"), dict) else {}
        if current.get("target_sha") == target and current.get("mode", mode) == mode and current.get("state") in {"queued", "running", "distributing", "restarting", "success"}:
            return {"accepted": True, "status": current}
        raise HTTPException(409, str(response.get("reason") or "Follower updater rejected release"))
    return {"accepted": True, "target_sha": target, "mode": mode}


@cluster_router.post("/status")
async def status(request: Request):
    relation = await authenticated(request)
    require_master_relation(relation)
    value = await agent_request({"action": "status"})
    if value.get("ok") and isinstance(value.get("status"), dict):
        try:
            return {"status": value["status"], "capabilities": p.read_capabilities(value)}
        except p.ProtocolError:
            pass
    # An agent socket gap during immutable handoff is an unavailable probe,
    # never evidence that the privately negotiated profile/capability changed.
    # Preflight still rejects it; only active convergence retries HTTP 503.
    raise HTTPException(503, "Follower updater temporarily unavailable")


router.include_router(cluster_router)
router.include_router(backup_control_router)
router.include_router(media_control_router)
router.include_router(playback_diagnostics_router)
