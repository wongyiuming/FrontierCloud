"""Identity-preserving offline handover. No initializer, migrations or Web API."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlsplit

from sqlalchemy import select, text, update

from app.core import db
from app.core.config import SECRET_DIR, settings
from app.services import runtime_gate
from app.services.certmanager.contract import hostname
from app.services.federation import protocol as p, schema as s
from app.services.federation.state import State
from app.services.federation.transport import Transport

IDLE_QUERIES = (
    "SELECT COUNT(*) FROM media_delete_operations WHERE state<>'rename_done'",
    "SELECT COUNT(*) FROM cluster_upload_sessions WHERE state NOT IN ('complete','cancelled')",
    "SELECT COUNT(*) FROM global_media_objects WHERE state<>'active'",
    "SELECT COUNT(*) FROM karaoke_recordings WHERE state NOT IN ('ready','deleted')",
    "SELECT COUNT(*) FROM karaoke_users WHERE status NOT IN ('active','banned','deleted')",
    "SELECT COUNT(*) FROM cluster_storage_members WHERE reserved_bytes<>0",
    "SELECT COUNT(*) FROM cluster_business_backups WHERE state NOT IN ('ready','failed')",
    "SELECT COUNT(*) FROM cluster_worker_jobs WHERE state NOT IN ('queued','complete')",
)


async def idle(conn):
    for statement in IDLE_QUERIES:
        if int(await conn.scalar(text(statement)) or 0):
            raise p.ProtocolError("Offline handover requires converged uploads, deletes, recordings and backups")


def arguments_valid(confirm: str, previous: str, endpoint: str):
    if (not p.IDENTIFIER.fullmatch(confirm) or not previous or previous == endpoint
            or len(previous) > 512 or len(endpoint) > 512 or p.endpoint(endpoint) != endpoint):
        raise p.ProtocolError("Invalid identity-preserving offline handover arguments")
    hostname(urlsplit(endpoint).hostname)
    port = urlsplit(endpoint).port or 443
    if port != 443 and not 1024 <= port <= 65535:
        raise p.ProtocolError("Storage HTTPS must use Direct 443 or a restricted Relay high port")


def validate_identity(store, row, confirm, role):
    if row["node_id"] != confirm or row["role"] != role:
        raise p.ProtocolError("Confirmed local NodeID or role does not match the existing identity")
    p.public_key(store.unseal(row["private_key"]))


async def move(store: State, confirm: str, previous: str, endpoint: str):
    arguments_valid(confirm, previous, endpoint)
    async with store.database.begin() as conn:
        row = await store.lock(conn)
        validate_identity(store, row, confirm, "Follower")
        if row["endpoint"] not in {previous, endpoint}:
            raise p.ProtocolError("Stored previous endpoint changed; handover compare-and-swap refused")
        await idle(conn)
        if row["endpoint"] == endpoint:
            return
        await conn.execute(update(s.identity).where(s.identity.c.singleton == 1, s.identity.c.node_id == confirm,
                            s.identity.c.endpoint == previous).values(endpoint=endpoint))
        await store.log(conn, "storage-endpoint-moved", "offline-storage-handover", previous=previous, next=endpoint)


async def rebind(store: State, transport: Transport, confirm: str, relationship: str, previous: str, endpoint: str):
    arguments_valid(confirm, previous, endpoint)
    if not p.IDENTIFIER.fullmatch(relationship):
        raise p.ProtocolError("storage-rebind requires the exact active relationship ID")
    async with store.database.connect() as conn:
        node = await store.lock(conn)
        validate_identity(store, node, confirm, "Master")
        await idle(conn)
        value = (await conn.execute(select(s.relationships).where(s.relationships.c.relationship_id == relationship))).mappings().first()
        expected = dict(value) if value else None
    if not expected or expected["direction"] != "downstream" or expected["state"] != "active" or expected["peer_endpoint"] not in {previous, endpoint}:
        raise p.ProtocolError("Relationship or its previous endpoint changed")
    if expected["mode"] == "Direct" and (urlsplit(endpoint).port or 443) != 443:
        raise p.ProtocolError("Direct storage must retain its public HTTPS 443 endpoint")
    identity = await transport.identity(endpoint, expected_id=expected["peer_id"], expected_key=expected["peer_key"], role="Follower")
    if (identity["node_id"] != expected["peer_id"] or identity["public_key"] != expected["peer_key"]
            or identity["role"] != "Follower" or p.endpoint(identity["endpoint"]) != endpoint):
        raise p.ProtocolError("New TLS endpoint did not prove the existing Follower identity")
    async with store.database.begin() as conn:
        current_node = await store.lock(conn)
        validate_identity(store, current_node, confirm, "Master")
        current = (await conn.execute(select(s.relationships).where(s.relationships.c.relationship_id == relationship).with_for_update())).mappings().first()
        if (current_node["private_key"] != node["private_key"] or not current
                or current["direction"] != "downstream" or current["state"] != "active"
                or any(current[key] != expected[key] for key in ("peer_id", "peer_key", "credential", "mode"))
                or current["peer_endpoint"] not in {previous, endpoint}):
            raise p.ProtocolError("Identity, key, credential or relationship changed during verified handover")
        await idle(conn)
        if current["peer_endpoint"] == endpoint:
            return
        await conn.execute(update(s.relationships).where(s.relationships.c.relationship_id == relationship).values(
            peer_endpoint=endpoint, status="offline", last_heartbeat=0))
        await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == current["peer_id"]).values(
            health="offline", writable=0, updated_at=int(time.time())))
        await store.log(conn, "storage-endpoint-rebound", "offline-storage-handover", relationship,
                        peer_id=current["peer_id"], previous=previous, next=endpoint)


def existing_key() -> bytes:
    path = SECRET_DIR / "node-vault.key"
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
        raise p.ProtocolError("Existing node vault must be a private regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        value = stream.read(65)
    if len(value) != 44:
        raise p.ProtocolError("Existing node vault is missing or invalid; identity is never regenerated")
    return value


async def execute(options):
    marker = runtime_gate.DATA_ROOT / ".frontiercloud-native-maintenance"
    if not stat.S_ISREG(marker.lstat().st_mode):
        raise p.ProtocolError("A hard operator maintenance fence is required")
    if settings.DB_TYPE == "sqlite":
        path = Path(settings.SQLITE_PATH)
        if runtime_gate.DATA_ROOT not in path.parents or not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size == 0 or path.stat().st_nlink != 1:
            raise p.ProtocolError("Open an existing persistent SQLite database within DATA_ROOT; initialization is forbidden")
        for parent in path.parents:
            if parent == runtime_gate.DATA_ROOT:
                break
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise p.ProtocolError("SQLite path cannot escape through a symbolic link")
    key = existing_key()
    if not settings.TLS_ENABLED:
        raise p.ProtocolError("Fixed cluster roles require certificate-verified TLS")
    if options.command == "storage-endpoint" and (settings.DEPLOYMENT_MODE != "only_stroge" or p.endpoint(settings.STORAGE_ENDPOINT) != options.endpoint):
        raise p.ProtocolError("Configure the storage appliance for its new endpoint before offline handover")
    with runtime_gate.acquire(create=False):
        try:
            from app.services import media_publication_recovery
            if media_publication_recovery.pending(runtime_gate.DATA_ROOT / "media"):
                raise p.ProtocolError("Pending media publication requires normal recovery before offline storage handover")
            async with db.engine.connect() as conn:
                await db._validate_initialized_schema(conn, await db._table_names(conn))
            store = State(db.engine, key)
            if options.command == "storage-endpoint":
                if options.relationship:
                    raise p.ProtocolError("storage-endpoint accepts no relationship argument")
                await move(store, options.confirm_node_id, options.previous_endpoint, options.endpoint)
            else:
                transport = Transport()
                try:
                    await rebind(store, transport, options.confirm_node_id, options.relationship, options.previous_endpoint, options.endpoint)
                finally:
                    await transport.close()
        finally:
            # No SQLite pool file descriptors may outlive the exclusive lease.
            await db.engine.dispose()


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("storage-endpoint", "storage-rebind"))
    parser.add_argument("--confirm-node-id", required=True)
    parser.add_argument("--previous-endpoint", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--relationship", default="")
    options = parser.parse_args(arguments)
    async def run():
        try:
            await execute(options)
        finally:
            await db.engine.dispose()
    try:
        asyncio.run(run())
        print(json.dumps({"endpoint": options.endpoint, "identity_preserved": True, "maintenance_enabled": True, "native_quiescent": True}))
        return 0
    except Exception as error:
        detail = str(error) if isinstance(error, (p.ProtocolError, runtime_gate.RuntimeBusy)) else type(error).__name__
        print("Offline storage handover failed: " + detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
