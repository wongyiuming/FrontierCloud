"""Master-managed whole releases; HTTP callers cannot provide publication input."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat

from app.core.config import settings
from app.services import release_control as legacy
from app.services.cluster_manifest_coordinator import _PIN_FIELDS, _same_manifest, peer_profile
from app.services.federation import protocol as p
from app.services.federation import release_manifest as manifests
from updater.release_evidence import github_get, verify_artifact

_BUSY = {"queued", "running", "distributing", "restarting"}


def read_published() -> dict:
    path = Path(settings.RELEASE_MANIFEST_PATH)
    if not path.is_absolute() or len(str(path)) > 4096:
        raise p.ProtocolError("Publication metadata path must be absolute")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= manifests.MAX_MANIFEST_BYTES:
        raise p.ProtocolError("Publication metadata must be a bounded regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(info, opened):
            raise p.ProtocolError("Publication metadata changed while opening")
        raw = stream.read(manifests.MAX_MANIFEST_BYTES + 1)
        after = os.fstat(stream.fileno())
        if opened.st_mtime_ns != after.st_mtime_ns or opened.st_size != after.st_size:
            raise p.ProtocolError("Publication metadata changed while reading")
    return manifests.parse(raw)


def verify_joint(manifest: dict, mode: str) -> None:
    for branch in ("main", "gin_main"):
        verify_artifact(manifest, branch)
        if mode == "upgrade":
            head = github_get("/branches/" + branch)
            if head.get("commit", {}).get("sha") != manifest["artifacts"][branch]["commit_sha"]:
                raise p.ProtocolError("Publication metadata is not both current production HEADs")


def ready(local: dict, followers: list[dict], manifest: dict) -> bool:
    try:
        if peer_profile(local, manifest) != legacy.RELEASE_BRANCH:
            return False
        for follower in followers:
            if not follower.get("reachable"):
                return False
            peer_profile(follower, manifest)
        return True
    except (ValueError, TypeError):
        return False


def converged(value: dict, manifest: dict) -> bool:
    try:
        branch = peer_profile(value, manifest)
        status = value["status"]
        return (status.get("state") == "success"
                and status.get("current_sha") == manifest["artifacts"][branch]["commit_sha"]
                and _same_manifest(status.get("current_manifest"), manifests.identifier(manifest)))
    except (ValueError, TypeError):
        return False


async def local_status() -> dict:
    value = await legacy.agent_request({"action": "status"})
    if value.get("ok") and isinstance(value.get("status"), dict):
        return {"status": value["status"], "capabilities": p.read_capabilities(value)}
    return {"status": {"state": "unavailable"}, "capabilities": []}


async def selection(identity: dict) -> dict:
    fresh = await legacy.state.read_existing_identity()
    if fresh.get("role") != "Master" or any(fresh.get(key) != identity.get(key) for key in ("node_id", "private_key")):
        raise p.ProtocolError("Master authority changed during release")
    rows = {row["relationship_id"]: row for row in await legacy.state.list_relationships()
            if row["direction"] == "downstream" and row["state"] == "active"}
    if len(rows) > 1000:
        raise p.ProtocolError("Too many release Followers")
    return rows


async def check_selection(identity: dict, peers: dict) -> None:
    fresh = await selection(identity)
    if fresh.keys() != peers.keys() or any(any(fresh[key].get(field) != peer.get(field) for field in _PIN_FIELDS) for key, peer in peers.items()):
        raise p.ProtocolError("Release relationship selection changed")


async def start(mode: str) -> dict:
    if mode not in {"upgrade", "rollback"}:
        raise p.ProtocolError("Invalid release mode")
    identity = await legacy.state.read_existing_identity()
    peers = await selection(identity)
    local = await local_status()
    status = local["status"]
    if status.get("state") in _BUSY:
        raise p.ProtocolError("Release already running")
    manifest = await asyncio.to_thread(read_published) if mode == "upgrade" else manifests.parse(p.canonical(status.get("previous_manifest")))
    async with asyncio.timeout(30):
        await asyncio.to_thread(verify_joint, manifest, mode)
    followers = await legacy.follower_release_statuses()
    if not ready(local, followers, manifest):
        raise p.ProtocolError("Updater capability or private profile unavailable")
    if mode == "upgrade" and converged(local, manifest) and all(converged(value, manifest) for value in followers):
        raise p.ProtocolError("Published whole release already converged")
    await check_selection(identity, peers)
    current = await local_status()
    if current["status"].get("state") in _BUSY or not ready(current, [], manifest):
        raise p.ProtocolError("Updater changed during release inspection")
    if mode == "rollback" and not _same_manifest(current["status"].get("previous_manifest"), manifests.identifier(manifest)):
        raise p.ProtocolError("Previous whole release changed")
    await check_selection(identity, peers)
    response = await legacy.agent_request({"action": "start", "release_manifest": manifest, "mode": mode, "hold_maintenance": True})
    if response.get("ok") is not True or response.get("release_id") != manifests.identifier(manifest):
        raise p.ProtocolError("Updater did not acknowledge whole release")
    return response


async def release_status() -> dict:
    identity = await legacy.state.read_existing_identity()
    local = await local_status()
    followers = await legacy.follower_release_statuses()
    manifest, verified = None, False
    try:
        manifest = await asyncio.to_thread(read_published)
        async with asyncio.timeout(30):
            await asyncio.to_thread(verify_joint, manifest, "upgrade")
        verified = True
    except (OSError, ValueError, TimeoutError):
        pass
    policy_ready = manifest is not None and ready(local, followers, manifest)
    complete = manifest is not None and converged(local, manifest) and all(converged(value, manifest) for value in followers)
    status = local["status"]
    try:
        previous = manifests.parse(p.canonical(status.get("previous_manifest")))
        rollback_ready = ready(local, followers, previous)
    except (ValueError, TypeError):
        rollback_ready = False
    fresh = await legacy.state.read_existing_identity()
    if any(fresh.get(key) != identity.get(key) for key in ("node_id", "role", "private_key")):
        policy_ready, rollback_ready = False, False
    return {"role": fresh.get("role"), "release_branch": legacy.RELEASE_BRANCH, "local": status, "followers": followers,
            "release_manifest": manifest, "ci": {"available": manifest is not None, "publishable": verified,
            "sha": manifest["artifacts"][legacy.RELEASE_BRANCH]["commit_sha"] if manifest else ""},
            "release_policy_ready": policy_ready, "release_policy_detail": "whole manifest requires both exact reviewed CI proofs and updater capabilities",
            "cluster_convergence_needed": not complete,
            "can_upgrade": fresh.get("role") == "Master" and policy_ready and verified and not complete and status.get("state") not in _BUSY,
            "can_rollback": fresh.get("role") == "Master" and rollback_ready and status.get("state") not in _BUSY}
