"""Durable node state. Each mutation and its audit share a transaction."""
from __future__ import annotations

import os
import secrets
import tempfile
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import delete, insert, select, update, tuple_

from app.core.config import SECRET_DIR
from app.core.db import engine
from . import protocol as p
from . import schema as s


def vault_key(directory: Path) -> bytes:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "node-vault.key"
    if not destination.exists():
        descriptor, name = tempfile.mkstemp(prefix=".node-vault-", dir=directory)
        temporary = Path(name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    return destination.read_bytes()


class State:
    def __init__(self, database=engine, key: bytes | None = None):
        self.database = database
        self._vault = Fernet(key) if key else None
        self.node: dict = {"role": "Standalone", "node_id": "", "endpoint": "", "catalog_version": 0}

    def seal(self, value: str) -> str:
        return self._vault.encrypt(value.encode()).decode()

    def unseal(self, value: str) -> str:
        return self._vault.decrypt(value.encode()).decode()

    async def lock(self, conn) -> dict:
        row = (await conn.execute(select(s.identity).where(s.identity.c.singleton == 1).with_for_update())).mappings().first()
        if not row:
            raise p.ProtocolError("Node is not initialized")
        return dict(row)

    async def initialize(self):
        if self._vault is None:
            self._vault = Fernet(vault_key(SECRET_DIR))
        async with self.database.begin() as conn:
            row = (await conn.execute(select(s.identity).where(s.identity.c.singleton == 1))).mappings().first()
            if row is None:
                row = dict(singleton=1, node_id=uuid.uuid4().hex, role="Standalone", endpoint="",
                           private_key=self.seal(p.new_key()), catalog_version=0, created_at=int(time.time()))
                await conn.execute(insert(s.identity).values(**row))
            # Fail closed if the persistent secret volume is missing or mismatched.
            self.unseal(row["private_key"])
            self.node = dict(row)

    def identity(self, challenge: str) -> dict:
        if not p.IDENTIFIER.fullmatch(challenge):
            raise p.ProtocolError("Invalid identity challenge")
        private = self.unseal(self.node["private_key"])
        return p.sign(private, {"node_id": self.node["node_id"], "role": self.node["role"],
                               "endpoint": self.node["endpoint"], "public_key": p.public_key(private),
                               "challenge": challenge, "protocol": p.PROTOCOL_VERSION,
                               "app_version": p.APP_VERSION})

    async def log(self, conn, action: str, actor: str, relationship: str | None = None, **detail):
        await conn.execute(insert(s.audit).values(audit_id=uuid.uuid4().hex, action=action,
            relationship_id=relationship, actor=actor[:128], detail=detail, created_at=int(time.time())))

    async def promote(self, role: str, endpoint: str, actor: str):
        if role not in ("Master", "Slave"):
            raise p.ProtocolError("Role must be Master or Slave")
        endpoint = p.endpoint(endpoint)
        async with self.database.begin() as conn:
            row = await self.lock(conn)
            if row["role"] != "Standalone":
                raise p.ProtocolError("节点角色已固定；仅显式重新初始化可重置")
            await conn.execute(update(s.identity).where(s.identity.c.singleton == 1).values(role=role, endpoint=endpoint))
            await self.log(conn, "promote", actor, role=role, endpoint=endpoint)
            row.update(role=role, endpoint=endpoint)
        self.node = row

    async def reset(self, actor: str, confirmation: str):
        async with self.database.begin() as conn:
            row = await self.lock(conn)
            if confirmation != row["node_id"]:
                raise p.ProtocolError("请准确输入当前节点 ID 确认重新初始化")
            # Retain credentials in encrypted revocation tombstones for peer notification.
            await conn.execute(update(s.relationships).where(s.relationships.c.state != "revoked").values(state="revoked", status="offline"))
            await conn.execute(update(s.pairs).values(state="revoked"))
            await conn.execute(delete(s.catalog))
            await conn.execute(delete(s.exports))
            row.update(node_id=uuid.uuid4().hex, role="Standalone", endpoint="", private_key=self.seal(p.new_key()), catalog_version=0)
            await conn.execute(update(s.identity).where(s.identity.c.singleton == 1).values(**{k: v for k, v in row.items() if k != "singleton"}))
            await self.log(conn, "reinitialize", actor, old_node_id=confirmation, new_node_id=row["node_id"])
        self.node = row

    async def list_relationships(self, include_revoked=False) -> list[dict]:
        async with self.database.connect() as conn:
            query = select(s.relationships).order_by(s.relationships.c.created_at, s.relationships.c.relationship_id)
            if not include_revoked:
                query = query.where(s.relationships.c.state != "revoked")
            return [dict(row) for row in (await conn.execute(query)).mappings()]

    async def relationship(self, identifier: str) -> dict:
        if not p.IDENTIFIER.fullmatch(identifier):
            raise p.ProtocolError("Invalid relationship")
        async with self.database.connect() as conn:
            row = (await conn.execute(select(s.relationships).where(s.relationships.c.relationship_id == identifier))).mappings().first()
        if not row:
            raise p.ProtocolError("Unknown relationship")
        return dict(row)

    async def create_pair(self, actor: str) -> dict:
        now, nonce, token = int(time.time()), uuid.uuid4().hex, secrets.token_urlsafe(48)
        async with self.database.begin() as conn:
            row = await self.lock(conn)
            if row["role"] != "Slave":
                raise p.ProtocolError("Only Slave can issue pairing packages")
            await conn.execute(delete(s.pairs).where(s.pairs.c.expires_at < now - 86400))
            await conn.execute(insert(s.pairs).values(nonce=nonce, token_hash=p.digest(token),
                expires_at=now + p.PAIR_SECONDS, state="issued"))
            await self.log(conn, "pair-issued", actor, nonce=nonce, expires_at=now + p.PAIR_SECONDS)
        private = self.unseal(row["private_key"])
        return p.sign(private, {"node_id": row["node_id"], "endpoint": row["endpoint"],
            "public_key": p.public_key(private), "token": token, "nonce": nonce,
            "expires_at": now + p.PAIR_SECONDS, "protocol": p.PROTOCOL_VERSION})

    def relation_values(self, identifier: str, peer: dict, credential: str, direction: str) -> dict:
        return dict(relationship_id=identifier, peer_id=peer["node_id"], peer_endpoint=p.endpoint(peer["endpoint"]),
            peer_key=peer["public_key"], credential=self.seal(credential), direction=direction, mode="Relay",
            state="pending", status="offline", last_heartbeat=0, rtt_ms=0, failures=0, recoveries=0,
            cursor=0, peer_version=peer["app_version"], protocol=p.PROTOCOL_VERSION, summary={}, created_at=int(time.time()))

    async def prepare(self, identifier: str, peer: dict, credential: str, actor: str):
        async with self.database.begin() as conn:
            row = await self.lock(conn)
            if row["role"] != "Master":
                raise p.ProtocolError("Only Master can import a package")
            existing = (await conn.execute(select(s.relationships).where(s.relationships.c.peer_id == peer["node_id"]))).mappings().first()
            if existing and existing["state"] != "revoked":
                raise p.ProtocolError("该 Slave 已配对或正在配对；请先撤销旧关系")
            if existing:
                await conn.execute(delete(s.catalog).where(s.catalog.c.relationship_id == existing["relationship_id"]))
                await conn.execute(delete(s.relationships).where(s.relationships.c.relationship_id == existing["relationship_id"]))
            await conn.execute(insert(s.relationships).values(**self.relation_values(identifier, peer, credential, "downstream")))
            await self.log(conn, "pair-prepared", actor, identifier, peer_id=peer["node_id"])

    async def consume(self, package: dict, identifier: str, master: dict, credential: str):
        if not p.IDENTIFIER.fullmatch(identifier) or len(p.decode(credential)) != 48:
            raise p.ProtocolError("Invalid pairing credentials")
        async with self.database.begin() as conn:
            row = await self.lock(conn)
            if row["role"] != "Slave" or package["node_id"] != row["node_id"]:
                raise p.ProtocolError("Pair package does not belong to this Slave")
            pair = (await conn.execute(select(s.pairs).where(s.pairs.c.nonce == package["nonce"]))).mappings().first()
            if (not pair or pair["state"] != "issued" or pair["expires_at"] <= int(time.time())
                    or not secrets.compare_digest(pair["token_hash"], p.digest(package["token"]))):
                raise p.ProtocolError("配对包已使用、过期或已撤销")
            old = (await conn.execute(select(s.relationships).where(s.relationships.c.peer_id == master["node_id"]))).mappings().first()
            if old and old["state"] != "revoked":
                raise p.ProtocolError("Master relationship already exists")
            if old:
                await conn.execute(delete(s.relationships).where(s.relationships.c.relationship_id == old["relationship_id"]))
            await conn.execute(insert(s.relationships).values(**self.relation_values(identifier, master, credential, "upstream")))
            await conn.execute(update(s.pairs).where(s.pairs.c.nonce == pair["nonce"]).values(
                state="consumed", relationship_id=identifier, master_id=master["node_id"]))
            await self.log(conn, "pair-consumed", master["node_id"], identifier)

    async def activate(self, identifier: str, actor: str):
        async with self.database.begin() as conn:
            await self.lock(conn)
            row = (await conn.execute(select(s.relationships).where(s.relationships.c.relationship_id == identifier))).mappings().first()
            if not row or row["state"] == "revoked":
                raise p.ProtocolError("Relationship revoked or unknown")
            if row["state"] != "active":
                await conn.execute(update(s.relationships).where(s.relationships.c.relationship_id == identifier).values(state="active"))
                await self.log(conn, "pair-activated", actor, identifier)

    async def revoke(self, identifier: str, actor: str):
        async with self.database.begin() as conn:
            await self.lock(conn)
            result = await conn.execute(update(s.relationships).where(s.relationships.c.relationship_id == identifier).values(state="revoked", status="offline"))
            if not result.rowcount:
                raise p.ProtocolError("Unknown relationship")
            await self.log(conn, "relationship-revoked", actor, identifier)

    async def set_mode(self, identifier: str, mode: str, actor: str):
        if mode not in ("Relay", "Direct"):
            raise p.ProtocolError("Mode must be Relay or Direct")
        async with self.database.begin() as conn:
            node = await self.lock(conn)
            if node["role"] != "Master":
                raise p.ProtocolError("Only Master selects transport")
            result = await conn.execute(update(s.relationships).where(
                s.relationships.c.relationship_id == identifier, s.relationships.c.state == "active").values(mode=mode))
            if not result.rowcount:
                raise p.ProtocolError("No active relationship")
            await self.log(conn, "mode-changed", actor, identifier, mode=mode)

    async def authenticate(self, headers, method: str, path: str, body: bytes, allow_pending=False, allow_revoked=False) -> dict:
        row = await self.relationship(headers.get("x-node-relationship", ""))
        allowed = {"active"} | ({"pending"} if allow_pending else set()) | ({"revoked"} if allow_revoked else set())
        if row["state"] not in allowed or row["protocol"] != p.PROTOCOL_VERSION:
            raise p.ProtocolError("Relationship not active")
        now = int(time.time())
        nonce = p.verify_auth(self.unseal(row["credential"]), headers, method, path, body, now)
        async with self.database.begin() as conn:
            await self.lock(conn)
            # Recheck revocation under the same lock as nonce insertion.
            current = (await conn.execute(select(s.relationships.c.state).where(s.relationships.c.relationship_id == row["relationship_id"]))).scalar_one()
            if current not in allowed:
                raise p.ProtocolError("Relationship revoked")
            await conn.execute(delete(s.requests).where(s.requests.c.expires_at <= now))
            used = (await conn.execute(select(s.requests.c.nonce).where(
                s.requests.c.relationship_id == row["relationship_id"], s.requests.c.nonce == nonce))).first()
            if used:
                raise p.ProtocolError("Replayed relationship request")
            await conn.execute(insert(s.requests).values(relationship_id=row["relationship_id"], nonce=nonce, expires_at=now + 120))
        return row

    async def heartbeat(self, identifier: str, success: bool, rtt: int = 0, summary: dict | None = None):
        async with self.database.begin() as conn:
            await self.lock(conn)
            row = (await conn.execute(select(s.relationships).where(s.relationships.c.relationship_id == identifier))).mappings().first()
            if not row or row["state"] != "active":
                return
            now = int(time.time())
            if success:
                values = dict(status="online", failures=0, last_heartbeat=now, rtt_ms=max(0, rtt),
                    recoveries=row["recoveries"] + int(row["status"] != "online" and row["last_heartbeat"] != 0), summary=summary or {})
            else:
                values = dict(failures=row["failures"] + 1,
                    status="offline" if now - row["last_heartbeat"] >= p.OFFLINE_SECONDS else "degraded")
            await conn.execute(update(s.relationships).where(s.relationships.c.relationship_id == identifier).values(**values))
            if values["status"] != row["status"]:
                await self.log(conn, "relationship-status", "heartbeat", identifier, previous=row["status"], current=values["status"])

    async def cleanup_playback_events(self):
        async with self.database.begin() as conn:
            expired = list((await conn.execute(select(s.events.c.session_id, s.events.c.resource_id).where(
                s.events.c.expires_at <= int(time.time())).limit(500))).all())
            if expired:
                await conn.execute(delete(s.events).where(tuple_(s.events.c.session_id, s.events.c.resource_id).in_(expired)))


state = State()
