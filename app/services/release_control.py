"""Web-facing release control and cached GitHub Actions verification."""
from __future__ import annotations

import asyncio
import json
import re
import socket
import time

import httpx

from app.core.config import settings
from app.services.federation.runtime import runtime
from app.services.federation.state import state

CONTROL_SOCKET = "/run/frontiercloud-updater/control.sock"
RELEASE_BRANCH = "main"
CI_BRANCH = "dev"
REPOSITORY_FULL_NAME = "wongyiuming/FrontierCloud"
REPOSITORY_API = f"https://api.github.com/repos/{REPOSITORY_FULL_NAME}"
CI_URL = f"{REPOSITORY_API}/actions/workflows/docker.yml/runs"
BRANCH_URL = f"{REPOSITORY_API}/branches/{RELEASE_BRANCH}"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CI_CACHE_SECONDS = 300
GITHUB_FAILURE_BACKOFF_SECONDS = 30
_ci_cache: tuple[float, dict] = (0.0, {})
_ci_last_verified: dict = {}
_github_backoff_until = 0.0
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


def _matching_ci_run(runs: list[dict], source_sha: str) -> dict | None:
    """Return the exact dev push CI run for one reviewed PR head commit."""
    if not SHA_RE.fullmatch(source_sha):
        return None
    for item in runs:
        if not isinstance(item, dict):
            continue
        if (
            item.get("head_branch") == CI_BRANCH
            and item.get("event") == "push"
            and str(item.get("head_sha") or "") == source_sha
        ):
            return item
    return None


def _promotion_source_sha(pulls: list[dict]) -> str:
    """Resolve exactly one merged same-repository dev -> main PR head SHA."""
    matches: set[str] = set()
    for item in pulls:
        if not isinstance(item, dict) or not item.get("merged_at"):
            continue
        base = item.get("base") if isinstance(item.get("base"), dict) else {}
        head = item.get("head") if isinstance(item.get("head"), dict) else {}
        head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
        source_sha = str(head.get("sha") or "")
        if (
            base.get("ref") == RELEASE_BRANCH
            and head.get("ref") == CI_BRANCH
            and head_repo.get("full_name") == REPOSITORY_FULL_NAME
            and SHA_RE.fullmatch(source_sha)
        ):
            matches.add(source_sha)
    return next(iter(matches)) if len(matches) == 1 else ""


def _int_header(response: httpx.Response | object, name: str) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _rate_limit_info(response: httpx.Response | object) -> dict:
    return {
        "limit": _int_header(response, "x-ratelimit-limit"),
        "remaining": _int_header(response, "x-ratelimit-remaining"),
        "used": _int_header(response, "x-ratelimit-used"),
        "reset_at": _int_header(response, "x-ratelimit-reset"),
    }


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "FrontierCloud-release-control",
    }
    token = settings.GITHUB_API_TOKEN.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _verification_metadata(response: httpx.Response | object | None = None) -> dict:
    value = {
        "authenticated": bool(settings.GITHUB_API_TOKEN.strip()),
        "checked_at": int(time.time()),
    }
    if response is not None:
        rate_limit = _rate_limit_info(response)
        if any(item is not None for item in rate_limit.values()):
            value["rate_limit"] = rate_limit
    return value


def _last_verified_snapshot(value: dict) -> dict:
    keys = (
        "branch", "source_branch", "sha", "tree_sha", "ci_sha", "status", "conclusion",
        "run_number", "html_url", "updated_at", "publishable", "checked_at", "authenticated",
    )
    return {key: value[key] for key in keys if key in value}


def _remember_verified(value: dict) -> None:
    global _ci_last_verified
    if value.get("available"):
        _ci_last_verified = _last_verified_snapshot(value)


def _retry_after_seconds(response: httpx.Response | object, now_epoch: int) -> int:
    retry_after = _int_header(response, "retry-after")
    reset_at = _int_header(response, "x-ratelimit-reset")
    candidates = [GITHUB_FAILURE_BACKOFF_SECONDS]
    if retry_after is not None and retry_after > 0:
        candidates.append(retry_after)
    if reset_at is not None and reset_at > now_epoch:
        candidates.append(reset_at - now_epoch)
    return max(candidates)


def _failure_value(
    *,
    error_kind: str,
    detail: str,
    http_status: int | None = None,
    response: httpx.Response | object | None = None,
    retry_after_seconds: int | None = None,
) -> dict:
    value = {
        "available": False,
        "branch": RELEASE_BRANCH,
        "source_branch": CI_BRANCH,
        "status": "unavailable",
        "conclusion": None,
        "detail": detail,
        "error_kind": error_kind,
        "publishable": False,
        **_verification_metadata(response),
    }
    if http_status is not None:
        value["http_status"] = http_status
    if retry_after_seconds is not None:
        value["retry_after_seconds"] = max(0, retry_after_seconds)
    if _ci_last_verified:
        value["last_verified"] = dict(_ci_last_verified)
    return value


def _backoff_value(now_epoch: int) -> dict:
    retry_after = max(0, int(_github_backoff_until - now_epoch))
    cached = _ci_cache[1]
    detail = cached.get("detail") if isinstance(cached, dict) else None
    value = _failure_value(
        error_kind="rate_limited",
        detail=str(detail or "GitHub API rate limit backoff is active"),
        http_status=cached.get("http_status") if isinstance(cached, dict) else None,
        retry_after_seconds=retry_after,
    )
    if isinstance(cached, dict) and isinstance(cached.get("rate_limit"), dict):
        value["rate_limit"] = dict(cached["rate_limit"])
    return value


def _github_message(response: httpx.Response | object) -> str:
    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    message = str(payload.get("message") or "").strip().replace("\n", " ")
    return message[:240]


async def ci_status(*, force: bool = False) -> dict:
    global _ci_cache, _github_backoff_until
    now = time.monotonic()
    now_epoch = int(time.time())
    if _github_backoff_until > now_epoch:
        value = _backoff_value(now_epoch)
        _ci_cache = (now, value)
        return value
    if not force and _ci_cache[1] and now - _ci_cache[0] < CI_CACHE_SECONDS:
        return _ci_cache[1]
    async with _ci_lock:
        now = time.monotonic()
        now_epoch = int(time.time())
        if _github_backoff_until > now_epoch:
            value = _backoff_value(now_epoch)
            _ci_cache = (now, value)
            return value
        if not force and _ci_cache[1] and now - _ci_cache[0] < CI_CACHE_SECONDS:
            return _ci_cache[1]
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(5, connect=3),
                headers=_github_headers(),
            ) as client:
                branch_response = await client.get(BRANCH_URL)
                branch_response.raise_for_status()
                branch_payload = branch_response.json()
                main_commit = branch_payload.get("commit") if isinstance(branch_payload.get("commit"), dict) else {}
                main_sha = str(main_commit.get("sha") or "")
                commit_payload = main_commit.get("commit") if isinstance(main_commit.get("commit"), dict) else {}
                tree_payload = commit_payload.get("tree") if isinstance(commit_payload.get("tree"), dict) else {}
                main_tree = str(tree_payload.get("sha") or "")
                source_sha = ""
                if SHA_RE.fullmatch(main_sha):
                    pulls_response = await client.get(f"{REPOSITORY_API}/commits/{main_sha}/pulls")
                    pulls_response.raise_for_status()
                    pulls_payload = pulls_response.json()
                    pulls = pulls_payload if isinstance(pulls_payload, list) else []
                    source_sha = _promotion_source_sha(pulls)

                if not SHA_RE.fullmatch(main_sha) or not SHA_RE.fullmatch(main_tree):
                    value = {
                        "available": False,
                        "branch": RELEASE_BRANCH,
                        "source_branch": CI_BRANCH,
                        "status": "unknown",
                        "conclusion": None,
                        "detail": "main HEAD tree is unavailable",
                        "publishable": False,
                        **_verification_metadata(branch_response),
                    }
                elif not SHA_RE.fullmatch(source_sha):
                    value = {
                        "available": True,
                        "branch": RELEASE_BRANCH,
                        "source_branch": CI_BRANCH,
                        "sha": main_sha,
                        "tree_sha": main_tree,
                        "status": "unknown",
                        "conclusion": None,
                        "detail": "main HEAD has no unique merged dev->main PR association",
                        "publishable": False,
                        **_verification_metadata(branch_response),
                    }
                else:
                    runs_response, source_response = await asyncio.gather(
                        client.get(CI_URL, params={"event": "push", "head_sha": source_sha, "per_page": 20}),
                        client.get(f"{REPOSITORY_API}/commits/{source_sha}"),
                    )
                    runs_response.raise_for_status()
                    source_response.raise_for_status()
                    runs = runs_response.json().get("workflow_runs") or []
                    source_payload = source_response.json()
                    source_commit = source_payload.get("commit") if isinstance(source_payload.get("commit"), dict) else {}
                    source_tree_payload = source_commit.get("tree") if isinstance(source_commit.get("tree"), dict) else {}
                    source_tree = str(source_tree_payload.get("sha") or "")
                    run = _matching_ci_run(runs, source_sha)
                    metadata = _verification_metadata(runs_response)

                    if source_tree != main_tree:
                        value = {
                            "available": True,
                            "branch": RELEASE_BRANCH,
                            "source_branch": CI_BRANCH,
                            "sha": main_sha,
                            "tree_sha": main_tree,
                            "ci_sha": source_sha,
                            "status": "unknown",
                            "conclusion": None,
                            "detail": "main HEAD tree differs from the reviewed dev PR tree",
                            "publishable": False,
                            **metadata,
                        }
                    elif not run:
                        value = {
                            "available": True,
                            "branch": RELEASE_BRANCH,
                            "source_branch": CI_BRANCH,
                            "sha": main_sha,
                            "tree_sha": main_tree,
                            "ci_sha": source_sha,
                            "status": "unknown",
                            "conclusion": None,
                            "detail": "reviewed dev PR head has no matching dev push CI result",
                            "publishable": False,
                            **metadata,
                        }
                    else:
                        completed = run.get("status") == "completed"
                        succeeded = run.get("conclusion") == "success"
                        detail = ""
                        if not completed:
                            detail = "dev CI for the reviewed PR head is still running"
                        elif not succeeded:
                            detail = f"dev CI for the reviewed PR head failed ({run.get('conclusion') or 'unknown'})"
                        value = {
                            "available": True,
                            "branch": RELEASE_BRANCH,
                            "source_branch": CI_BRANCH,
                            "sha": main_sha,
                            "tree_sha": main_tree,
                            "ci_sha": source_sha,
                            "status": run.get("status"),
                            "conclusion": run.get("conclusion"),
                            "run_number": run.get("run_number"),
                            "html_url": run.get("html_url"),
                            "updated_at": run.get("updated_at"),
                            "detail": detail,
                            "publishable": completed and succeeded,
                            **metadata,
                        }
                _github_backoff_until = 0.0
                _remember_verified(value)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            status_code = int(response.status_code)
            rate_limit = _rate_limit_info(response)
            rate_limited = status_code == 429 or (status_code == 403 and rate_limit.get("remaining") == 0)
            message = _github_message(response)
            if rate_limited:
                retry_after = _retry_after_seconds(response, now_epoch)
                _github_backoff_until = float(now_epoch + retry_after)
                limit = rate_limit.get("limit")
                remaining = rate_limit.get("remaining")
                quota = ""
                if limit is not None or remaining is not None:
                    quota = f" ({remaining if remaining is not None else '?'} / {limit if limit is not None else '?' } remaining)"
                value = _failure_value(
                    error_kind="rate_limited",
                    detail=f"GitHub API rate limited: HTTP {status_code}{quota}",
                    http_status=status_code,
                    response=response,
                    retry_after_seconds=retry_after,
                )
            else:
                detail = f"GitHub API HTTP {status_code}"
                if message:
                    detail += f": {message}"
                value = _failure_value(
                    error_kind="http_status",
                    detail=detail,
                    http_status=status_code,
                    response=response,
                )
        except httpx.RequestError as exc:
            value = _failure_value(
                error_kind="network",
                detail=f"GitHub API network error: {type(exc).__name__}",
            )
        except Exception as exc:
            value = _failure_value(
                error_kind="unexpected",
                detail=f"GitHub API verification failed: {type(exc).__name__}",
            )
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
