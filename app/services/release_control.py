"""Web-facing release control and cached GitHub Actions status."""
from __future__ import annotations

import asyncio
import json
import re
import socket
import time

import httpx

from app.services.federation.runtime import runtime
from app.services.federation.state import state

CONTROL_SOCKET = "/run/frontiercloud-updater/control.sock"
CI_URL = "https://api.github.com/repos/wongyiuming/FrontierCloud/actions/workflows/docker.yml/runs"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CI_CACHE_SECONDS = 90
_ci_cache: tuple[float, dict] = (0.0, {})
_ci_lock = asyncio.Lock()


def _agent_request(payload: dict) -> dict:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(CONTROL_SOCKET)
        connection.sendall(encoded)
        stream = connection.makefile("rb")
        raw = stream.readline(65536)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("invalid updater response")
    return value


async def agent_request(payload: dict) -> dict:
    try:
        return await asyncio.to_thread(_agent_request, payload)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"ok": False, "reason": f"updater unavailable: {type(exc).__name__}"}


async def agent_status() -> dict:
    response = await agent_request({"action": "status"})
    status = response.get("status") if response.get("ok") else None
    return status if isinstance(status, dict) else {
        "state": "unavailable", "phase": "unavailable", "detail": response.get("reason", "updater unavailable")
    }


async def ci_status(*, force: bool = False) -> dict:
    global _ci_cache
    now = time.monotonic()
    if not force and _ci_cache[1] and now - _ci_cache[0] < CI_CACHE_SECONDS:
        return _ci_cache[1]
    async with _ci_lock:
        now = time.monotonic()
        if not force and _ci_cache[1] and now - _ci_cache[0] < CI_CACHE_SECONDS:
            return _ci_cache[1]
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(5, connect=3),
                headers={"Accept": "application/vnd.github+json", "User-Agent": "FrontierCloud-release-control"},
            ) as client:
                response = await client.get(CI_URL, params={"branch": "dev", "event": "push", "per_page": 5})
                response.raise_for_status()
                runs = response.json().get("workflow_runs") or []
            run = next((item for item in runs if item.get("head_branch") == "dev"), None)
            if not run:
                value = {"available": False, "status": "unknown", "conclusion": None, "detail": "dev CI run not found"}
            else:
                sha = str(run.get("head_sha") or "")
                value = {
                    "available": bool(SHA_RE.fullmatch(sha)),
                    "sha": sha,
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "run_number": run.get("run_number"),
                    "html_url": run.get("html_url"),
                    "updated_at": run.get("updated_at"),
                    "publishable": bool(SHA_RE.fullmatch(sha)) and run.get("status") == "completed" and run.get("conclusion") == "success",
                }
        except Exception as exc:
            value = {"available": False, "status": "unavailable", "conclusion": None,
                     "detail": f"GitHub Actions unavailable: {type(exc).__name__}", "publishable": False}
        _ci_cache = (time.monotonic(), value)
        return value


async def follower_release_statuses() -> list[dict]:
    if state.node.get("role") != "Master":
        return []
    relations = [row for row in await state.list_relationships()
                 if row["direction"] == "downstream" and row["state"] == "active"]

    async def one(relation: dict) -> dict:
        base = {"relationship_id": relation["relationship_id"], "peer_id": relation["peer_id"],
                "peer_endpoint": relation["peer_endpoint"]}
        try:
            value = await runtime.call(relation, "/internal/v1/cluster-update/status", {})
            status = value.get("status") if isinstance(value, dict) else None
            return {**base, "reachable": True, "status": status if isinstance(status, dict) else {}}
        except Exception as exc:
            return {**base, "reachable": False, "status": {}, "detail": type(exc).__name__}

    return await asyncio.gather(*(one(row) for row in relations))


async def release_status(*, refresh_ci: bool = False) -> dict:
    local, ci = await asyncio.gather(agent_status(), ci_status(force=refresh_ci))
    followers = await follower_release_statuses()
    target = str(ci.get("sha") or "")
    current = str(local.get("current_sha") or "")
    busy = local.get("state") in {"queued", "running", "distributing"}
    return {
        "role": state.node.get("role"),
        "ci": ci,
        "local": local,
        "followers": followers,
        "can_upgrade": state.node.get("role") == "Master" and bool(ci.get("publishable"))
                       and target != current and not busy,
        "can_rollback": state.node.get("role") == "Master" and bool(local.get("previous_sha")) and not busy,
    }


async def start_upgrade() -> dict:
    if state.node.get("role") != "Master":
        raise RuntimeError("Only Master can start a cluster release")
    ci = await ci_status(force=True)
    target = str(ci.get("sha") or "")
    if not ci.get("publishable") or not SHA_RE.fullmatch(target):
        raise RuntimeError("Latest dev CI is not publishable")
    local = await agent_status()
    if target == local.get("current_sha"):
        raise RuntimeError("Latest tested dev commit is already running")
    response = await agent_request({
        "action": "start", "target_sha": target, "mode": "upgrade", "hold_maintenance": True,
    })
    if not response.get("ok"):
        raise RuntimeError(str(response.get("reason") or "updater rejected release"))
    return response


async def start_rollback() -> dict:
    if state.node.get("role") != "Master":
        raise RuntimeError("Only Master can roll back the cluster")
    local = await agent_status()
    target = str(local.get("previous_sha") or "")
    if not SHA_RE.fullmatch(target):
        raise RuntimeError("No previous Web-managed release is available")
    response = await agent_request({
        "action": "start", "target_sha": target, "mode": "rollback", "hold_maintenance": True,
    })
    if not response.get("ok"):
        raise RuntimeError(str(response.get("reason") or "updater rejected rollback"))
    return response
