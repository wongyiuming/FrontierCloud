"""Bounded whole-manifest convergence over the existing authenticated plane."""
from __future__ import annotations

import asyncio
import sys

import httpx

from sqlalchemy import text

from app.core.db import close_db
from app.services.federation import protocol as p
from app.services.federation import release_manifest as manifests
from app.services.federation.runtime import runtime
from app.services.federation.state import state
from app.services.federation.transport import ControlHTTPError, transport

POLL_SECONDS = 4
TIMEOUT_SECONDS = 900
_PIN_FIELDS = ("relationship_id", "peer_id", "peer_endpoint", "peer_key", "credential", "direction", "state", "protocol")


def peer_profile(value: dict, manifest: dict) -> str:
    if manifests.MANIFEST_CAPABILITY not in p.read_capabilities(value):
        raise p.ProtocolError("Follower lacks manifest capability")
    status = value.get("status")
    if not isinstance(status, dict):
        raise p.ProtocolError("Follower updater is unavailable")
    branch = status.get("release_branch")
    source = {"main": "dev", "gin_main": "gin_dev"}.get(branch)
    manifests.select(manifest, branch, source)
    return branch


def _same_manifest(value, release_id: str) -> bool:
    try:
        return manifests.identifier(value) == release_id
    except (ValueError, TypeError):
        return False


async def converge(manifest: dict, mode: str) -> None:
    # This copy bounds and freezes caller input before any network operation.
    manifest = manifests.parse(p.canonical(manifest))
    if mode not in {"upgrade", "rollback"}:
        raise p.ProtocolError("Invalid release mode")
    release_id = manifests.identifier(manifest)
    identity = await state.read_existing_identity()
    if identity["role"] != "Master":
        raise p.ProtocolError("Only Master can distribute a manifest")
    peers = {row["relationship_id"]: row for row in await state.list_relationships()
             if row["direction"] == "downstream" and row["state"] == "active"}
    if len(peers) > 1000:
        raise p.ProtocolError("Too many release Followers")
    slots = asyncio.Semaphore(4)

    async def check():
        fresh = await state.read_existing_identity()
        if any(fresh.get(key) != identity.get(key) for key in ("node_id", "role", "private_key")):
            raise p.ProtocolError("Master authority changed during release")
        rows = {row["relationship_id"]: row for row in await state.list_relationships()
                if row["direction"] == "downstream" and row["state"] == "active"}
        if rows.keys() != peers.keys() or any(
            any(rows[key].get(field) != peer.get(field) for field in _PIN_FIELDS)
            for key, peer in peers.items()
        ):
            raise p.ProtocolError("Release relationship selection changed")

    async def call(peer, path, payload):
        async with slots:
            await check()
            fresh = await state.relationship(peer["relationship_id"])
            if any(fresh.get(field) != peer.get(field) for field in _PIN_FIELDS):
                raise p.ProtocolError("Release relationship pin changed")
            async with asyncio.timeout(10):
                return await runtime.call(fresh, path, payload)

    async with asyncio.timeout(TIMEOUT_SECONDS):
        # All capabilities/profiles must pass before the first start command.
        values = await asyncio.gather(*(call(peer, "/internal/v1/cluster-update/status", {}) for peer in peers.values()))
        profiles = {key: peer_profile(value, manifest) for key, value in zip(peers, values)}
        await check()

        async def start(peer):
            value = await call(peer, "/internal/v1/cluster-update/start", {"release_manifest": manifest, "mode": mode})
            if value.get("accepted") is not True or value.get("release_id") != release_id:
                raise p.ProtocolError("Follower manifest acknowledgement missing")

        await asyncio.gather(*(start(peer) for peer in peers.values()))
        while True:
            await check()

            async def probe(key, peer):
                try:
                    value = await call(peer, "/internal/v1/cluster-update/status", {})
                except (OSError, TimeoutError, httpx.HTTPError):
                    return False
                except ControlHTTPError as exc:
                    # Replacement/drain can temporarily gate status through
                    # Nginx. Authentication, malformed payloads and private
                    # profile/authority changes remain fail-closed.
                    if exc.status_code in {502, 503, 504}:
                        return False
                    raise
                branch = peer_profile(value, manifest)
                if branch != profiles[key]:
                    raise p.ProtocolError("Follower private profile changed")
                status = value["status"]
                if _same_manifest(status.get("target_manifest"), release_id) and status.get("state") == "failed":
                    raise p.ProtocolError("Follower manifest release failed")
                return (status.get("state") == "success"
                        and status.get("current_sha") == manifest["artifacts"][branch]["commit_sha"]
                        and _same_manifest(status.get("current_manifest"), release_id))

            completed = await asyncio.gather(*(probe(key, peer) for key, peer in peers.items()))
            await check()
            if all(completed):
                return
            await asyncio.sleep(POLL_SECONDS)


async def run(encoded: str, mode: str) -> None:
    manifest = manifests.parse(p.decode(encoded))
    try:
        # Never initialize/migrate a database or generate an identity here.
        async with state.database.connect() as conn:
            if await conn.scalar(text("SELECT generation FROM frontiercloud_schema WHERE singleton=1")) != 2:
                raise p.ProtocolError("Release requires existing schema generation 2")
        await state.load_existing_identity()
        transport.open()
        await converge(manifest, mode)
    finally:
        await transport.close()
        await close_db()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: cluster_manifest_coordinator <encoded-manifest> <upgrade|rollback>")
    try:
        asyncio.run(run(sys.argv[1], sys.argv[2]))
    except Exception as exc:
        print(f"cluster manifest release failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
