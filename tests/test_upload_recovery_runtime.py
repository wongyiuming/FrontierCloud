"""Real SQLite + authenticated HTTPS-shaped ASGI upload recovery on Linux."""
import asyncio
import hashlib
import json
import os
import secrets
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import insert, select, text, update
from sqlalchemy.engine import URL
from starlette.requests import ClientDisconnect

from app.api import internal_nodes, internal_storage_integrity as storage
from app.api.v1 import admin_cluster_integrity as cluster
from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_manager, resource_pool, upload_claims
from app.services.federation import protocol as p, schema as s
from app.services.federation.state import State


@unittest.skipIf(os.name == "nt", "Product upload leases use Linux kernel flock")
class UploadRecoveryRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=media_manager.MEDIA_ROOT.parent)
        self.root = Path(self.temporary.name)
        self.master_root, self.follower_root = self.root / "master/media", self.root / "follower/media"
        for root in (self.master_root, self.follower_root):
            for folder in ("music", "vido", "lyrics"):
                (root / folder).mkdir(parents=True)
        self.databases = []
        for role in ("master", "follower"):
            database = create_sqlite_engine(URL.create("sqlite+aiosqlite", database=str(self.root / role / "state.db")))
            self.databases.append(database)
            with patch.object(db, "engine", database), patch.object(db, "SECRET_DIR", self.root / role / "secrets"):
                await db.init_db()
        self.master = State(self.databases[0], Fernet.generate_key())
        self.follower = State(self.databases[1], Fernet.generate_key())
        await self.master.initialize()
        await self.follower.initialize()
        with patch("app.api.v1.media.MEDIA_ROOT", self.master_root):
            await self.master.promote("Master", "https://master.4399.com", "test", 2 * 1024 ** 3)
        await self.follower.promote("Follower", "https://storage.4399.com", "test")
        self.relationship, self.credential = uuid.uuid4().hex, secrets.token_urlsafe(48)
        package = await self.follower.create_pair("test")
        signed_master = self.master.identity(uuid.uuid4().hex)
        master_identity = p.verify(signed_master["payload"]["public_key"], signed_master)
        await self.follower.consume(package["payload"], self.relationship, master_identity, self.credential)
        await self.follower.activate(self.relationship, "test")
        signed_follower = self.follower.identity(uuid.uuid4().hex)
        follower_identity = p.verify(signed_follower["payload"]["public_key"], signed_follower)
        await self.master.prepare(self.relationship, follower_identity, self.credential, "test")
        await self.master.activate(self.relationship, "test")
        await self.master.heartbeat(self.relationship, True)
        await self.follower.heartbeat(self.relationship, True)
        async with self.follower.database.begin() as conn:
            await conn.execute(insert(s.storage_members).values(member_id=self.follower.node["node_id"], relationship_id=self.relationship, member_kind="Follower", transport="Direct", storage_enabled=1, allocated_bytes=2 * 1024 ** 3, used_bytes=0, reserved_bytes=0, physical_free_bytes=2 * 1024 ** 3, health="online", writable=1, updated_at=int(time.time())))
        async with self.master.database.begin() as conn:
            await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == self.follower.node["node_id"]).values(storage_enabled=1, allocated_bytes=2 * 1024 ** 3, physical_free_bytes=2 * 1024 ** 3, health="online", writable=1))
        self.patches = [patch.object(storage, "state", self.follower), patch.object(internal_nodes, "state", self.follower), patch.object(storage, "MEDIA_ROOT", self.follower_root), patch.object(media_manager, "MEDIA_ROOT", self.follower_root), patch.object(internal_nodes.settings, "TLS_ENABLED", True), patch.object(upload_claims, "ROOT", self.root / "leases"), patch.object(cluster, "node_state", self.master), patch.object(cluster, "MEDIA_ROOT", self.master_root), patch.object(cluster, "invalidate_media_catalog", new=AsyncMock()), patch.object(cluster.admin_service, "audit", new=AsyncMock())]
        for item in self.patches:
            item.start()
        app = FastAPI()
        app.include_router(storage.router, prefix="/internal/v1")
        self.app = app
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://storage.4399.com")
        self.data = b"ID3" + b"f" * 1021
        self.path = "music/artist/song.mp3"

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        for database in self.databases:
            await database.dispose()
        self.temporary.cleanup()

    def token(self, original, size=None, *, encrypted=False, operation="upload"):
        return p.storage_token(self.credential, self.relationship, self.master.node["node_id"], self.follower.node["node_id"], original, original, operation, self.path, len(self.data) if size is None else size, int(time.time()), encrypted=encrypted)

    async def control(self, original, suffix, payload=None):
        path = f"/internal/v1/storage/{original}/{suffix}"
        body = p.canonical(payload or {"path": self.path, "expected_bytes": len(self.data)})
        return await self.client.post(path, content=body, headers=p.auth_headers(self.credential, self.relationship, "POST", path, body))

    async def remote_call(self, relation, path, payload):
        body = p.canonical(payload)
        response = await self.client.post(path, content=body, headers=p.auth_headers(self.credential, relation["relationship_id"], "POST", path, body))
        if response.status_code != 200:
            raise p.ProtocolError(f"HTTP {response.status_code}")
        return response.json()

    async def counts(self, database, member):
        async with database.connect() as conn:
            return (await conn.execute(select(s.storage_members.c.used_bytes, s.storage_members.c.reserved_bytes).where(s.storage_members.c.member_id == member))).one()

    async def reserve_master(self, *, local=False):
        member = self.master.node["node_id"] if local else self.follower.node["node_id"]
        return await cluster._reserve_upload(self.path, len(self.data), member)

    async def age(self, database, upload_id):
        async with database.begin() as conn:
            await conn.execute(update(s.upload_sessions).where(s.upload_sessions.c.upload_id == upload_id).values(expires_at=1, updated_at=1))

    async def test_live_writer_survives_expired_deadline_and_reconcile(self):
        original = secrets.token_hex(32)
        ready, finish = asyncio.Event(), asyncio.Event()
        async def body():
            yield self.data[:100]
            ready.set()
            await finish.wait()
            yield self.data[100:]
        task = asyncio.create_task(self.client.put(f"/internal/v1/storage/{original}", content=body(), headers={"X-Storage-Capability": self.token(original)}))
        await ready.wait()
        await self.age(self.follower.database, storage.session_id(original))
        result = await self.control(original, "reconcile", {"path": self.path, "expected_bytes": len(self.data), "expired_only": True})
        self.assertEqual(result.json()["state"], "busy")
        self.assertEqual(await self.counts(self.follower.database, self.follower.node["node_id"]), (0, len(self.data)))
        self.assertEqual(await storage.cleanup_expired_storage(), 0)
        finish.set()
        response = await task
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.counts(self.follower.database, self.follower.node["node_id"]), (len(self.data), 0))

    async def test_browser_disconnect_cancels_durable_quota_and_reupload_succeeds(self):
        original = secrets.token_hex(32)
        async def body():
            yield self.data[:100]
            raise ClientDisconnect()
        with self.assertRaises(ClientDisconnect):
            await self.client.put(f"/internal/v1/storage/{original}", content=body(), headers={"X-Storage-Capability": self.token(original)})
        self.assertEqual(await self.counts(self.follower.database, self.follower.node["node_id"]), (0, 0))
        self.assertFalse((self.follower_root / f".cluster-upload-{original}.part").exists())
        self.assertEqual((await storage._session(original))["state"], "cancelled")
        fresh = secrets.token_hex(32)
        response = await self.client.put(f"/internal/v1/storage/{fresh}", content=self.data, headers={"X-Storage-Capability": self.token(fresh)})
        self.assertEqual(response.status_code, 200, response.text)

    async def test_reconcile_tombstone_rejects_capability_arriving_after_release(self):
        original = secrets.token_hex(32)
        token = self.token(original)
        response = await self.control(original, "reconcile")
        self.assertTrue(response.json()["cleared"])
        late = await self.client.put(f"/internal/v1/storage/{original}", content=self.data, headers={"X-Storage-Capability": token})
        self.assertEqual(late.status_code, 409)
        self.assertEqual(await self.counts(self.follower.database, self.follower.node["node_id"]), (0, 0))

    async def test_signed_ciphertext_is_opaque_and_plaintext_magic_stays_required(self):
        original, opaque = secrets.token_hex(32), b"opaque ciphertext" * 64
        response = await self.client.put(f"/internal/v1/storage/{original}", content=opaque, headers={"X-Storage-Capability": self.token(original, len(opaque), encrypted=True)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.follower_root / self.path).read_bytes(), opaque)
        deleted = await self.client.post(f"/internal/v1/storage/{original}/delete", headers={"X-Storage-Capability": self.token(original, len(opaque), encrypted=True, operation="delete")})
        self.assertEqual(deleted.status_code, 200)
        plain = secrets.token_hex(32)
        denied = await self.client.put(f"/internal/v1/storage/{plain}", content=opaque, headers={"X-Storage-Capability": self.token(plain, len(opaque))})
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(await self.counts(self.follower.database, self.follower.node["node_id"]), (0, 0))

    async def test_unauthenticated_reconcile_cannot_release_or_create_tombstone(self):
        original = secrets.token_hex(32)
        response = await self.client.post(f"/internal/v1/storage/{original}/reconcile", json={"path": self.path, "expected_bytes": len(self.data)})
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(await storage._session(original))

    async def test_restart_reaper_removes_abandoned_follower_partial_and_quota(self):
        original = secrets.token_hex(32)
        with upload_claims.claim(original):
            await storage._reserve(original, self.path, len(self.data))
            (self.follower_root / f".cluster-upload-{original}.part").write_bytes(self.data[:100])
        await self.age(self.follower.database, storage.session_id(original))
        self.assertEqual(await storage.cleanup_expired_storage(), 1)
        self.assertEqual(await self.counts(self.follower.database, self.follower.node["node_id"]), (0, 0))
        self.assertFalse((self.follower_root / f".cluster-upload-{original}.part").exists())

    async def test_postpublish_crash_recovers_ciphertext_without_releasing_completed_bytes(self):
        original, opaque = secrets.token_hex(32), b"ciphertext" * 128
        with upload_claims.claim(original):
            await storage._reserve(original, self.path, len(opaque), encrypted=True)
            target = self.follower_root / self.path
            target.parent.mkdir(parents=True)
            target.write_bytes(opaque)
        await self.age(self.follower.database, storage.session_id(original))
        self.assertEqual(await storage.cleanup_expired_storage(), 1)
        self.assertEqual(target.read_bytes(), opaque)
        self.assertEqual((await storage._session(original))["state"], "complete")
        self.assertEqual(await self.counts(self.follower.database, self.follower.node["node_id"]), (len(opaque), 0))

    async def test_master_remote_404_never_releases_without_authenticated_cleanup_proof(self):
        reservation = await self.reserve_master()
        await self.age(self.master.database, reservation["upload_id"])
        with patch.object(cluster.node_runtime, "call", new=AsyncMock(side_effect=p.ProtocolError("HTTP 404"))):
            self.assertFalse(await cluster._cleanup_upload_session(reservation["upload_id"], expired_only=True))
        self.assertEqual(await self.counts(self.master.database, self.follower.node["node_id"]), (0, len(self.data)))
        with patch.object(cluster.node_runtime, "call", new=self.remote_call):
            self.assertTrue(await cluster._cleanup_upload_session(reservation["upload_id"], expired_only=True))
        self.assertEqual(await self.counts(self.master.database, self.follower.node["node_id"]), (0, 0))
        self.assertEqual((await storage._session(reservation["media_id"]))["state"], "cancelled")
        await self.reserve_master()

    async def test_master_recovers_finished_direct_upload_after_browser_refresh(self):
        reservation = await self.reserve_master()
        original = reservation["media_id"]
        response = await self.client.put(f"/internal/v1/storage/{original}", content=self.data, headers={"X-Storage-Capability": self.token(original)})
        self.assertEqual(response.status_code, 200)
        await self.age(self.master.database, reservation["upload_id"])
        with patch.object(cluster.node_runtime, "call", new=self.remote_call):
            self.assertTrue(await cluster._cleanup_upload_session(reservation["upload_id"], expired_only=True))
        self.assertEqual((await resource_pool.upload_session(reservation["upload_id"], self.master.database))["state"], "complete")
        self.assertEqual(await self.counts(self.master.database, self.follower.node["node_id"]), (len(self.data), 0))
        self.assertEqual((self.follower_root / self.path).read_bytes(), self.data)

    async def test_master_local_partial_cleanup_and_full_publish_recovery(self):
        reservation = await self.reserve_master(local=True)
        part = self.master_root / f".cluster-upload-{reservation['upload_id']}.part"
        part.write_bytes(self.data[:100])
        await self.age(self.master.database, reservation["upload_id"])
        with upload_claims.claim(reservation["upload_id"]):
            self.assertFalse(await cluster._cleanup_upload_session(reservation["upload_id"], allow_unindexed_local=True, expired_only=True))
        self.assertTrue(part.exists())
        self.assertTrue(await cluster._cleanup_upload_session(reservation["upload_id"], allow_unindexed_local=True, expired_only=True))
        self.assertFalse(part.exists())
        new = await self.reserve_master(local=True)
        target = self.master_root / self.path
        target.parent.mkdir(parents=True)
        target.write_bytes(self.data)
        await self.age(self.master.database, new["upload_id"])
        self.assertTrue(await cluster._cleanup_upload_session(new["upload_id"], allow_unindexed_local=True, expired_only=True))
        self.assertEqual(target.read_bytes(), self.data)
        self.assertEqual((await resource_pool.upload_session(new["upload_id"], self.master.database))["state"], "complete")
        self.assertEqual(await self.counts(self.master.database, self.master.node["node_id"]), (len(self.data), 0))

    async def test_master_restart_adoption_keeps_reservation_and_managed_hash(self):
        reservation = await self.reserve_master(local=True)
        target = self.master_root / self.path
        target.parent.mkdir(parents=True)
        target.write_bytes(self.data)
        with patch("app.api.v1.media.MEDIA_ROOT", self.master_root):
            await resource_pool.adopt_master_local_media(self.master.node, self.master.database)
        async with self.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.global_media.c.media_id).where(s.global_media.c.media_path == self.path)), None)
        self.assertEqual(await self.counts(self.master.database, self.master.node["node_id"]), (0, len(self.data)))
        await self.age(self.master.database, reservation["upload_id"])
        await cluster._cleanup_upload_session(reservation["upload_id"], allow_unindexed_local=True, expired_only=True)
        expected = '"' + hashlib.sha256(self.data).hexdigest() + '"'
        with patch("app.api.v1.media.MEDIA_ROOT", self.master_root):
            await resource_pool.adopt_master_local_media(self.master.node, self.master.database)
        self.assertEqual((await resource_pool.media(reservation["media_id"], self.master.database))["etag"], expected)

    async def test_relay_stream_holds_both_leases_and_finishes_after_master_deadline(self):
        await self.master.set_mode(self.relationship, "Relay", "test")
        await self.follower.accept_mode(self.relationship, "Relay", "test")
        async with self.master.database.begin() as conn:
            await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == self.follower.node["node_id"]).values(physical_free_bytes=2 * 1024 ** 3, writable=1))
        reservation = await self.reserve_master()
        entered, finish = asyncio.Event(), asyncio.Event()
        class Body:
            async def stream(body_self):
                yield self.data[:100]
                entered.set()
                await finish.wait()
                yield self.data[100:]
        original_client = httpx.AsyncClient
        def bridge(**options):
            self.assertFalse(options["trust_env"])
            import ssl
            self.assertEqual(options["verify"].verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(options["verify"].check_hostname)
            return original_client(transport=httpx.ASGITransport(app=self.app), base_url="https://storage.4399.com")
        with patch.object(cluster.httpx, "AsyncClient", new=bridge):
            task = asyncio.create_task(cluster.upload_session_bytes(reservation["upload_id"], Body(), "admin"))
            await entered.wait()
            await self.age(self.master.database, reservation["upload_id"])
            self.assertFalse(await cluster._cleanup_upload_session(reservation["upload_id"], expired_only=True))
            finish.set()
            result = await task
        self.assertEqual(result["media_id"], reservation["media_id"])
        self.assertEqual(await self.counts(self.master.database, self.follower.node["node_id"]), (len(self.data), 0))
        self.assertEqual(await self.counts(self.follower.database, self.follower.node["node_id"]), (len(self.data), 0))

    async def test_raw_put_inactivity_releases_follower_reservation(self):
        original = secrets.token_hex(32)
        async def body():
            yield self.data[:100]
            await asyncio.sleep(1)
            yield self.data[100:]
        with patch("app.core.config.settings.ADMIN_UPLOAD_INACTIVITY_TIMEOUT", 0.1):
            response = await self.client.put(f"/internal/v1/storage/{original}", content=body(), headers={"X-Storage-Capability": self.token(original)})
        self.assertEqual(response.status_code, 408)
        self.assertEqual(await self.counts(self.follower.database, self.follower.node["node_id"]), (0, 0))
        self.assertEqual((await storage._session(original))["state"], "cancelled")

    async def test_completed_ciphertext_rename_preserves_proof_and_signed_delete(self):
        from app.services import media_directories
        original = secrets.token_hex(32)
        self.data = b"opaque-ciphertext\0\xff" * 20
        response = await self.client.put(f"/internal/v1/storage/{original}", content=self.data,
                                        headers={"X-Storage-Capability": self.token(original, encrypted=True)})
        self.assertEqual(response.status_code, 200)
        with patch.object(media_directories, "MEDIA_ROOT", self.follower_root), patch.object(media_directories, "invalidate_media_catalog", new=AsyncMock()):
            await media_directories.rename_follower_directory("music/artist", "music/renamed", self.follower.database)
        self.path = "music/renamed/song.mp3"
        self.assertEqual((await storage._session(original))["media_path"], self.path)
        response = await self.control(original, "stat", {"path": self.path, "expected_bytes": len(self.data), "encrypted": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sha256"], hashlib.sha256(self.data).hexdigest())
        response = await self.client.post(f"/internal/v1/storage/{original}/delete",
                                         headers={"X-Storage-Capability": self.token(original, encrypted=True, operation="delete")})
        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.follower_root / self.path).exists())
        self.assertEqual(await self.counts(self.follower.database, self.follower.node["node_id"]), (0, 0))

    async def test_rename_metadata_failure_rolls_back_completed_upload_path_and_bytes(self):
        from app.services import media_directories
        original = secrets.token_hex(32)
        response = await self.client.put(f"/internal/v1/storage/{original}", content=self.data,
                                        headers={"X-Storage-Capability": self.token(original)})
        self.assertEqual(response.status_code, 200)
        async with self.follower.database.begin() as conn:
            await conn.execute(text("""CREATE TRIGGER reject_upload_rename BEFORE UPDATE OF media_path ON media_objects
                WHEN NEW.media_path LIKE 'music/renamed/%' BEGIN SELECT RAISE(ABORT,'rename metadata failure'); END"""))
        with patch.object(media_directories, "MEDIA_ROOT", self.follower_root), patch.object(media_directories, "invalidate_media_catalog", new=AsyncMock()):
            with self.assertRaises(Exception):
                await media_directories.rename_follower_directory("music/artist", "music/renamed", self.follower.database)
        self.assertEqual((await storage._session(original))["media_path"], self.path)
        self.assertEqual((self.follower_root / self.path).read_bytes(), self.data)
        self.assertFalse((self.follower_root / "music/renamed").exists())
        self.assertEqual((await self.control(original, "stat")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
