"""One bounded control loop per participating node; no media forwarding."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
import uuid
from contextlib import suppress

from app.core.config import settings

from . import protocol as p
from .state import state
from .transport import transport

logger = logging.getLogger("frontiercloud.nodes")
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
BACKUP_RETRY_SECONDS = 5 * 60
HEARTBEAT_WINDOW_SECONDS = 60 * 60
HEARTBEAT_SAMPLE_LIMIT = 180


def heartbeat_summary(previous: dict, rtt_ms: int, now: int | None = None) -> dict:
    """Keep a bounded one-hour RTT window inside the existing relationship JSON."""
    stamp = int(time.time()) if now is None else int(now)
    old = previous.get("heartbeat") if isinstance(previous, dict) else {}
    old = old if isinstance(old, dict) else {}
    samples = []
    for item in old.get("samples", []):
        if (isinstance(item, list) and len(item) == 2
                and isinstance(item[0], int) and isinstance(item[1], int)
                and stamp - HEARTBEAT_WINDOW_SECONDS <= item[0] <= stamp
                and item[1] >= 0):
            samples.append([item[0], item[1]])
    samples.append([stamp, max(0, int(rtt_ms))])
    samples = samples[-HEARTBEAT_SAMPLE_LIMIT:]
    values = [item[1] for item in samples]
    return {
        "current_ms": values[-1],
        "min_ms": min(values),
        "avg_ms": round(sum(values) / len(values)),
        "max_ms": max(values),
        "count": len(values),
        "window_seconds": HEARTBEAT_WINDOW_SECONDS,
        "samples": samples,
    }


class Runtime:
    def __init__(self):
        self.task = None
        self.wakeup = asyncio.Event()
        self.backup_attempts: dict[str, int] = {}
        self.worker_tasks: set[asyncio.Task] = set()

    def start(self, *, revocations=False):
        if state.node["role"] != "Standalone" and not settings.TLS_ENABLED:
            raise p.ProtocolError("固定为 Master/Follower 的节点必须启用有效 HTTPS；请恢复 TLS_ENABLED 后启动，不会自动重置角色")
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
        tasks = list(self.worker_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.worker_tasks.clear()
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
                expected_key=package["public_key"], role="Follower")
        except (KeyError, TypeError) as exc:
            raise p.ProtocolError("Invalid pairing package") from exc
        for old in await state.list_relationships(include_revoked=True):
            if (old["peer_id"] == peer["node_id"] and old["state"] == "revoked"
                    and not old["summary"].get("revocation_acknowledged")):
                await self.notify_revocation(old)
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
            logger.warning("Peer revocation pending", extra={"context": {"relationship_id": identifier}})
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
                return  # The Master owns confirmation; a pending Follower never routes.
        start = time.monotonic()
        try:
            # Check the pinned identity on recovery, not an arbitrary replacement endpoint.
            if relation["status"] != "online":
                await transport.identity(relation["peer_endpoint"], expected_id=relation["peer_id"],
                    expected_key=relation["peer_key"], role="Follower" if relation["direction"] == "downstream" else "Master")
            value = {}
            if relation["direction"] == "downstream":
                from app.services import resource_pool
                value = {"mode": relation["mode"],
                         "resources": await resource_pool.member_configuration(relation["peer_id"], state.database)}
            summary = await self.call(relation, "/internal/v1/heartbeat", value)
            if summary.get("protocol") != p.PROTOCOL_VERSION:
                raise p.ProtocolError("Heartbeat protocol mismatch")
            rtt_ms = int((time.monotonic() - start) * 1000)
            summary["heartbeat"] = heartbeat_summary(relation.get("summary") or {}, rtt_ms)
            await state.heartbeat(identifier, True, rtt_ms, summary)
            if relation["direction"] == "downstream":
                await self.maybe_backup(relation)
            elif relation["direction"] == "upstream":
                await self.fill_worker_slots(relation)
        except Exception:
            await state.heartbeat(identifier, False)
            raise

    async def maybe_backup(self, relation: dict):
        from app.services import resource_pool
        member = next((item for item in await resource_pool.list_members(state.database)
                       if item["member_id"] == relation["peer_id"]), None)
        if not member or not member["backup"].get("enabled"):
            return
        now = int(time.time())
        last_success = int(member["backup"].get("last_success") or 0)
        last_attempt = int(self.backup_attempts.get(relation["relationship_id"], 0))
        interval = BACKUP_INTERVAL_SECONDS if last_success >= last_attempt else BACKUP_RETRY_SECONDS
        if now - max(last_success, last_attempt) < interval:
            return
        self.backup_attempts[relation["relationship_id"]] = now
        generation, artifact, checksum = await resource_pool.build_business_backup(state.database)
        try:
            await self.call(relation, "/internal/v1/backup/begin", {"generation": generation})
            with artifact.open("rb") as source:
                chunk_index = 0
                while chunk := source.read(192 * 1024):
                    await self.call(relation, "/internal/v1/backup/chunk", {
                        "generation": generation, "chunk_index": chunk_index,
                        "chunk": base64.b64encode(chunk).decode("ascii"),
                    })
                    chunk_index += 1
            await self.call(relation, "/internal/v1/backup/commit", {
                "generation": generation, "checksum": checksum,
            })
        finally:
            artifact.unlink(missing_ok=True)

    async def execute_worker_job(self, relation: dict, job: dict) -> None:
        payload = job.get("payload") or {}
        object_id = str(payload.get("object_id") or "")
        from sqlalchemy import text
        async with state.database.connect() as conn:
            path = await conn.scalar(text("SELECT media_path FROM media_objects WHERE media_id=:id"), {"id": object_id})
        if not path:
            result = {"error": "object_not_found"}
        else:
            from app.api.v1.media import MEDIA_ROOT
            target = (MEDIA_ROOT / path).resolve()
            if MEDIA_ROOT not in target.parents or not target.is_file():
                result = {"error": "object_not_found"}
            else:
                info = target.stat()
                result = {"size_bytes": info.st_size, "updated_at": info.st_mtime_ns}
                if job["job_type"] == "hash":
                    digest = hashlib.sha256()
                    with target.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                    result["sha256"] = digest.hexdigest()
        await self.call(relation, f"/internal/v1/jobs/{job['job_id']}/complete",
                        {"lease": job["lease"], "result": result})

    def _worker_done(self, task: asyncio.Task) -> None:
        self.worker_tasks.discard(task)
        if not task.cancelled():
            try:
                error = task.exception()
            except Exception:
                error = None
            if error is not None:
                logger.warning("Compute worker task failed: %s", type(error).__name__)
        self.wakeup.set()

    async def fill_worker_slots(self, relation: dict) -> None:
        """Fill the configured worker concurrency instead of treating slots as display-only."""
        from app.services import resource_pool
        local = await resource_pool.follower_resource_summary(state.node, state.database)
        compute = local.get("compute") or {}
        if not compute.get("enabled"):
            return
        slots = max(0, int(compute.get("worker_slots") or 0))
        vacancies = max(0, slots - len(self.worker_tasks))
        if vacancies <= 0:
            return
        capabilities = compute.get("capabilities") or []
        for _ in range(vacancies):
            response = await self.call(relation, "/internal/v1/jobs/lease", {"capabilities": capabilities})
            job = response.get("job")
            if not isinstance(job, dict):
                break
            task = asyncio.create_task(
                self.execute_worker_job(relation, job),
                name=f"node-worker-{str(job.get('job_id') or '')[:8]}",
            )
            self.worker_tasks.add(task)
            task.add_done_callback(self._worker_done)

    async def work_once(self, relation: dict):
        """Backward-compatible entry point; now fills every configured free slot."""
        await self.fill_worker_slots(relation)

    async def run(self):
        try:
            while True:
                self.wakeup.clear()
                try:
                    await state.cleanup_playback_events()
                    if state.node["role"] == "Master":
                        from app.services import resource_pool
                        await resource_pool.retry_pending_deletes(state)
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
                                               extra={"context": {"relationship_id": relation["relationship_id"]}})
                    await asyncio.gather(*(checked_tick(row) for row in relations if row["state"] != "revoked" or row in pending))
                except Exception as exc:
                    logger.warning("Node resource control deferred: %s", type(exc).__name__)
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=p.HEARTBEAT_SECONDS)
                except TimeoutError:
                    pass
        finally:
            await transport.close()


runtime = Runtime()
