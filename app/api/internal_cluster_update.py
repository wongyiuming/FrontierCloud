"""Authenticated cluster update control delegated to the local updater container."""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import socket

from fastapi import APIRouter, HTTPException, Request

from app.api import internal_nodes
from app.services.federation.state import state

router = APIRouter(prefix="/internal/v1", include_in_schema=False)
SOCKET_PATH = "/run/frontiercloud-updater/control.sock"
STATUS_PATH = pathlib.Path("/run/frontiercloud-updater/status.json")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _enqueue(version: str) -> dict:
    payload = json.dumps({"version": version, "propagate": False}, separators=(",", ":")) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(SOCKET_PATH)
        client.sendall(payload.encode())
        raw = client.makefile("rb").readline(4096)
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise RuntimeError("Invalid updater response")
    return result


@router.post("/cluster-update")
async def cluster_update(request: Request):
    relation = await internal_nodes.authenticated(request)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only the paired Master may update a Follower")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        version = str(value["version"])
        if not SHA_RE.fullmatch(version):
            raise ValueError()
        result = await asyncio.to_thread(_enqueue, version)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid cluster update request") from exc
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        raise HTTPException(503, "Local updater is unavailable") from exc
    if not result.get("accepted"):
        raise HTTPException(409, str(result.get("reason") or "Update rejected"))
    return {"status": "queued", "version": version}


@router.post("/cluster-update/status")
async def cluster_update_status(request: Request):
    relation = await internal_nodes.authenticated(request)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only the paired Master may query a Follower update")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        version = str(value["version"])
        if not SHA_RE.fullmatch(version):
            raise ValueError()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid cluster update status request") from exc
    try:
        current = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(503, "Local updater status is unavailable") from exc
    return {"status": current.get("state"), "version": current.get("version"),
            "detail": current.get("detail", ""), "updated_at": current.get("updated_at")}
