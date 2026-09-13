"""One bounded control loop per participating node; no media forwarding."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from contextlib import suppress

from . import protocol as p
from .catalog import catalog
from .state import state
from .transport import transport

logger = logging.getLogger("frontiercloud.nodes")


class Runtime:
    def __init__(self):
        self.task = None
        self.wakeup = asyncio.Event()

    def start(self, *, revocations=False):
        if (state.node["role"] != "Standalone" or revocations) and (self.task is None or self.task.done()):
            transport.open()
            self.task = asyncio.create_task(self.run(), name="node-control")
        self.wakeup.set()

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        await transport.close()

    async def call(self, relation: dict, path: str, value=None):
        return await transport.request(relation["peer_endpoint"], path,
            method="POST" if value is not None else "GET", value=value,
            relation=relation["relationship_id"], credential=state.unseal(relation["credential"]))

    async def import_pair(self, envelope: dict, actor: str):
        try:
            package = p.verify(envelope["payload"]["public_key"], envelope)
            if package["protocol"] != p.PROTOCOL_VERSION or package["expires_at"] <= int(time.time()):
                raise p.ProtocolError("配对包过期或协议不兼容")
            peer = await transport.identity(package["endpoint"], expected_id=package["node_id"],
                expected_key=package["public_key"], role="Slave")
        except (KeyError, TypeError) as exc:
            raise p.ProtocolError("Invalid pairing package") from exc
        identifier, credential = uuid.uuid4().hex, secrets.token_urlsafe(48)
        await state.prepare(identifier, peer, credential, actor)
        relation = await state.relationship(identifier)
        # A durable pending record lets the loop finish activation after interruption.
        challenge = uuid.uuid4().hex
        await transport.request(peer["endpoint"], "/internal/v1/pair", method="POST", value={
            "package": envelope, "relationship_id": identifier, "credential": credential,
            "master": state.identity(challenge)})
        await self.call(relation, "/internal/v1/confirm", {})
        await state.activate(identifier, actor)
        self.start()
        return identifier

    async def revoke(self, identifier: str, actor: str):
        relation = await state.relationship(identifier)
        # Local trust is removed even if the peer is unreachable.
        await state.revoke(identifier, actor)
        try:
            await self.notify_revocation(relation)
        except Exception:
            logger.warning("Peer revocation pending", extra={"relationship_id": identifier})
        self.start()

    async def notify_revocation(self, relation):
        if relation["summary"].get("revocation_acknowledged"):
            return
        await self.call(relation, "/internal/v1/revoke", {})
        from sqlalchemy import update
        from . import schema as s
        async with state.database.begin() as conn:
            await state.lock(conn)
            await conn.execute(update(s.relationships).where(s.relationships.c.relationship_id == relation["relationship_id"],
                s.relationships.c.state == "revoked").values(summary={"revocation_acknowledged": True}))
            await state.log(conn, "revocation-acknowledged", "peer", relation["relationship_id"])

    async def sync(self, relation: dict):
        from app.services.media_catalog_cache import invalidate_media_catalog
        cursor, head = relation["cursor"], None
        # Bound one cycle; large inventories resume at the committed cursor.
        for _ in range(10):
            path = f"/internal/v1/catalog?cursor={cursor}" + (f"&head={head}" if head is not None else "")
            page = await self.call(relation, path)
            cursor = await catalog.apply(relation, page, cursor)
            head = page["head"]
            if page["items"]:
                await invalidate_media_catalog()
            if page["complete"]:
                break

    async def tick(self, relation: dict):
        identifier = relation["relationship_id"]
        if relation["state"] == "revoked":
            # Idempotent peer tombstones; retries use fresh nonce authentication.
            await self.notify_revocation(relation)
            return
        if relation["state"] == "pending":
            if int(time.time()) - relation["created_at"] > p.PAIR_SECONDS:
                await self.revoke(identifier, "pair-timeout")
                return
            if relation["direction"] == "downstream":
                await self.call(relation, "/internal/v1/confirm", {})
                await state.activate(identifier, "pair-recovery")
                relation = await state.relationship(identifier)
            else:
                return  # The Master owns confirmation; a pending Slave never routes.
        start = time.monotonic()
        try:
            # Check the pinned identity on recovery, not an arbitrary replacement endpoint.
            if relation["status"] != "online":
                await transport.identity(relation["peer_endpoint"], expected_id=relation["peer_id"],
                    expected_key=relation["peer_key"], role="Slave" if relation["direction"] == "downstream" else "Master")
            summary = await self.call(relation, "/internal/v1/heartbeat", {})
            if summary.get("protocol") != p.PROTOCOL_VERSION:
                raise p.ProtocolError("Heartbeat protocol mismatch")
            await state.heartbeat(identifier, True, int((time.monotonic() - start) * 1000), summary)
            if relation["direction"] == "downstream":
                await self.sync(relation)
        except Exception:
            await state.heartbeat(identifier, False)
            raise

    async def run(self):
        try:
            while True:
                self.wakeup.clear()
                try:
                    await catalog.scan()
                    await state.cleanup_playback_events()
                    relations = await state.list_relationships(include_revoked=True)
                    pending = [row for row in relations if row["state"] == "revoked" and not row["summary"].get("revocation_acknowledged")]
                    if state.node["role"] == "Standalone" and not pending:
                        break
                    semaphore = asyncio.Semaphore(4)
                    async def checked_tick(relation):
                        async with semaphore:
                            try:
                                await self.tick(relation)
                            except Exception as exc:
                                logger.warning("Node control operation deferred: %s", type(exc).__name__,
                                               extra={"relationship_id": relation["relationship_id"]})
                    await asyncio.gather(*(checked_tick(row) for row in relations if row["state"] != "revoked" or row in pending))
                except Exception as exc:
                    logger.warning("Node inventory deferred: %s", type(exc).__name__)
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=p.HEARTBEAT_SECONDS)
                except TimeoutError:
                    pass
        finally:
            await transport.close()


runtime = Runtime()
