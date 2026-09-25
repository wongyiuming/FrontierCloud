"""Web-facing release control and cached GitHub Actions verification."""
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
RELEASE_BRANCH = "main"
CI_BRANCH = "dev"
REPOSITORY_API = "https://api.github.com/repos/wongyiuming/FrontierCloud"
CI_URL = f"{REPOSITORY_API}/actions/workflows/docker.yml/runs"
BRANCH_URL = f"{REPOSITORY_API}/branches/{RELEASE_BRANCH}"
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


def _matching_ci_run(runs: list[dict], tree_sha: str) -> dict | None:
    """Return the newest dev push CI run whose tested Git tree matches main."""
    if not SHA_RE.fullmatch(tree_sha):
        return None
    for item in runs:
        head_commit = item.get("head_commit") if isinstance(item.get("head_commit"), dict) else {}
        if (
            item.get("head_branch") == CI_BRANCH
            and item.get("event") == "push"
            and str(head_commit.get("tree_id") or "") == tree_sha
        ):
            return item
    return None


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
                runs_response, branch_response = await asyncio.gather(
                    client.get(CI_URL, params={"branch": CI_BRANCH, "event": "push", "per_page": 100}),
                    client.get(BRANCH_URL),
                )
                runs_response.raise_for_status()
                branch_response.raise_for_status()
                runs = runs_response.json().get("workflow_runs") or []
                branch_payload = branch_response.json()
            main_commit = branch_payload.get("commit") if isinstance(branch_payload.get("commit"), dict) else {}
            main_sha = str(main_commit.get("sha") or "")
            commit_payload = main_commit.get("commit") if isinstance(main_commit.get("commit"), dict) else {}
            tree_payload = commit_payload.get("tree") if isinstance(commit_payload.get("tree"), dict) else {}
            main_tree = str(tree_payload.get("sha") or "")
            run = _matching_ci_run(runs, main_tree)
            if not SHA_RE.fullmatch(main_sha) or not SHA_RE.fullmatch(main_tree):
                value = {
                    "available": False,
                    "branch": RELEASE_BRANCH,
                    "source_branch": CI_BRANCH,
                    "status": "unknown",
                    "conclusion": None,
                    "detail": "main HEAD tree is unavailable",
                    "publishable": False,
                }
            elif not run:
                value = {
                    "available": True,
                    "branch": RELEASE_BRANCH,
                    "source_branch": CI_BRANCH,
                    "sha": main_sha,
                    "tree_sha": main_tree,
                    "status": "unknown",
                    "conclusion": None,
                    "detail": "main HEAD tree has no matching dev CI result",
                    "publishable": False,
                }
            else:
                ci_sha = str(run.get("head_sha") or "")
                completed = run.get("status") == "completed"
                succeeded = run.get("conclusion") == "success"
                detail = ""
                if not completed:
                    detail = "dev CI for the main HEAD tree is still running"
                elif not succeeded:
                    detail = f"dev CI for the main HEAD tree failed ({run.get('conclusion') or 'unknown'})"
                value = {
                    "available": True,
                    "branch": RELEASE_BRANCH,
                    "source_branch": CI_BRANCH,
                    "sha": main_sha,
                    "tree_sha": main_tree,
                    "ci_sha": ci_sha,
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "run_number": run.get("run_number"),
                    "html_url": run.get("html_url"),
                    "updated_at": run.get("updated_at"),
                    "detail": detail,
                    "publishable": completed and succeeded,
                }
        except Exception as exc:
            value = {
                "available": False,
                "branch": RELEASE_BRANCH,
                "source_branch": CI_BRANCH,
                "status": "unavailable",
                "conclusion": None,
                "detail": f"GitHub Actions unavailable: {type(exc).__name__}",
                "publishable": False,
            }
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


def updater_policy_status(local: dict, followers: list[dict]) -> tuple[bool, str]:
    if local.get("release_branch") != RELEASE_BRANCH:
        return False, "Master updater has not been migrated to main release policy"
    unready = []
    for item in followers:
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        if not item.get("reachable") or status.get("release_branch") != RELEASE_BRANCH:
            unready.append(str(item.get("peer_endpoint") or item.get("peer_id") or "Follower"))
    if unready:
        return False, "Follower updater has not been migrated to main release policy: " + ", ".join(unready)
    return True, ""


def followers_need_convergence(followers: list[dict], target: str) -> bool:
    if not target:
        return False
    for item in followers:
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        if not item.get("reachable") or status.get("current_sha") != target or status.get("state") != "success":
            return True
    return False


async def release_status(*, refresh_ci: bool = False) -> dict:
    local, ci = await asyncio.gather(agent_status(), ci_status(force=refresh_ci))
    followers = await follower_release_statuses()
    target = str(ci.get("sha") or "")
    current = str(local.get("current_sha") or "")
    busy = local.get("state") in {"queued", "running", "distributing"}
    policy_ready, policy_detail = updater_policy_status(local, followers)
    convergence_needed = followers_need_convergence(followers, target)
    return {
        "role": state.node.get("role"),
        "release_branch": RELEASE_BRANCH,
        "ci": ci,
        "local": local,
        "followers": followers,
        "release_policy_ready": policy_ready,
        "release_policy_detail": policy_detail,
        "cluster_convergence_needed": convergence_needed,
        "can_upgrade": state.node.get("role") == "Master" and policy_ready and bool(ci.get("publishable"))
                       and (target != current or convergence_needed) and not busy,
        "can_rollback": state.node.get("role") == "Master" and policy_ready
                        and bool(local.get("previous_sha")) and not busy,
    }


async def start_upgrade() -> dict:
    if state.node.get("role") != "Master":
        raise RuntimeError("Only Master can start a cluster release")
    ci = await ci_status(force=True)
    target = str(ci.get("sha") or "")
    if not ci.get("publishable") or not SHA_RE.fullmatch(target):
        raise RuntimeError("Current main HEAD does not match a successful dev CI tree")
    local = await agent_status()
    followers = await follower_release_statuses()
    policy_ready, policy_detail = updater_policy_status(local, followers)
    if not policy_ready:
        raise RuntimeError(policy_detail)
    if target == local.get("current_sha") and not followers_need_convergence(followers, target):
        raise RuntimeError("Latest tested main commit is already converged across the cluster")
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
    followers = await follower_release_statuses()
    policy_ready, policy_detail = updater_policy_status(local, followers)
    if not policy_ready:
        raise RuntimeError(policy_detail)
    target = str(local.get("previous_sha") or "")
    if not SHA_RE.fullmatch(target):
        raise RuntimeError("No previous Web-managed release is available")
    response = await agent_request({
        "action": "start", "target_sha": target, "mode": "rollback", "hold_maintenance": True,
    })
    if not response.get("ok"):
        raise RuntimeError(str(response.get("reason") or "updater rejected rollback"))
    return response
