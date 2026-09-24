"""Real transactional SQLite tests of the portable node tables and protocol."""
import asyncio
import hashlib
import secrets
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, insert, select, text, update
from sqlalchemy.dialects.mysql.dml import Insert as MySQLInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from starlette.requests import Request

from app.api.internal_nodes import require_https
from app.api import internal_nodes
from app.services.federation import protocol as p, schema as s, routing
from app.services.federation.catalog import Catalog
from app.services.federation.state import State, vault_key
from app.services.federation.runtime import Runtime
from app.services.federation.transport import Transport


class Connection:
    def __init__(self, connection):
        self.connection = connection
        self.dialect = connection.dialect

    async def scalar(self, statement, parameters=None):
        return self.connection.scalar(statement, parameters or {})

    async def execute(self, statement, parameters=None):
        if isinstance(statement, MySQLInsert):
            # Exercise the same unique-key semantics with SQLite's equivalent
            # syntax; real MySQL locking is covered by sql_concurrency_smoke.
            values = statement.compile().params
            names = statement.table.columns.keys()
            rows, index = [], 0
            while any(f"{name}_m{index}" in values for name in names):
                rows.append({name: values[f"{name}_m{index}"] for name in names if f"{name}_m{index}" in values})
                index += 1
            if not rows:
                rows = [{name: values[name] for name in names if name in values}]
            converted = sqlite_insert(statement.table).values(rows)
            if statement._post_values_clause is not None:
                columns = [column.name for column in statement.table.primary_key]
                converted = converted.on_conflict_do_update(index_elements=columns,
                    set_={name: converted.excluded[name] for name in rows[0] if name not in columns})
            else:
                converted = converted.prefix_with("OR IGNORE")
            statement = converted
        return self.connection.execute(statement, parameters or {})


class Transaction:
    def __init__(self, database, mutation):
        self.database, self.mutation = database, mutation

    async def __aenter__(self):
        await self.database.lock.acquire()
        self.context = self.database.engine.begin() if self.mutation else self.database.engine.connect()
        return Connection(self.context.__enter__())

    async def __aexit__(self, kind, value, traceback):
        try:
            return self.context.__exit__(kind, value, traceback)
        finally:
            self.database.lock.release()


class Database:
    def __init__(self):
        self.engine = create_engine("sqlite://")
        s.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text("""CREATE TABLE media_objects (
                media_id TEXT PRIMARY KEY, object_kind TEXT NOT NULL, media_path TEXT NOT NULL,
                path_locator TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""))
            conn.execute(text("""CREATE TABLE media_playback_stats (
                media_id TEXT PRIMARY KEY, media_path TEXT NOT NULL, play_score INTEGER NOT NULL DEFAULT 0,
                preference INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""))
            conn.execute(text("""CREATE TABLE media_playback_events (
                playback_session_id TEXT NOT NULL, media_id TEXT NOT NULL, counted_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, PRIMARY KEY (playback_session_id, media_id))"""))
            conn.execute(text("""CREATE TABLE media_lyric_links (
                media_id TEXT PRIMARY KEY, media_path TEXT NOT NULL, lyric_id TEXT NOT NULL,
                lyric_path TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""))
            conn.execute(text("""CREATE TABLE karaoke_recordings (
                recording_id TEXT PRIMARY KEY, storage_member_id TEXT, state TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0)"""))
        self.lock = asyncio.Lock()

    def begin(self):
        return Transaction(self, True)

    def connect(self):
        return Transaction(self, False)


def peer(role="Master", name="master"):
    private = p.new_key()
    return {"node_id": uuid.uuid4().hex, "endpoint": f"https://{name}.example.com", "role": role,
            "public_key": p.public_key(private), "app_version": p.APP_VERSION}


def media_payload(path="music/same/song.wav", preference=2, score=7):
    return {"path": path, "size": 4096, "updated_at": 1, "etag": '"1-1000"',
            "play_score": score, "preference": preference, "has_lyrics": True}


class NodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.media_root = Path(self.media_directory.name)
        for name in ("music", "vido", "lyrics"):
            (self.media_root / name).mkdir()
        self.media_patch = patch("app.api.v1.media.MEDIA_ROOT", self.media_root)
        self.media_patch.start()
        self.recording_patch = patch("app.services.karaoke_storage.ROOT", self.media_root / "recordings")
        self.recording_patch.start()
        self.database = Database()
        self.key = Fernet.generate_key()
        self.store = State(self.database, self.key)
        await self.store.initialize()

    async def asyncTearDown(self):
        self.database.engine.dispose()
        self.media_patch.stop()
        self.recording_patch.stop()
        self.media_directory.cleanup()

    async def follower(self):
        await self.store.promote("Follower", "https://follower.example.com", "admin")

    async def consumed(self, master=None):
        master = master or peer()
        package = await self.store.create_pair("admin")
        identifier, credential = uuid.uuid4().hex, secrets.token_urlsafe(48)
        await self.store.consume(package["payload"], identifier, master, credential)
        return identifier, credential, package, master

    async def test_promotion_persists_and_cannot_change_online(self):
        original = self.store.node["node_id"]
        await self.follower()
        restarted = State(self.database, self.key)
        await restarted.initialize()
        self.assertEqual(restarted.node["role"], "Follower")
        self.assertEqual(restarted.node["node_id"], original)
        with self.assertRaises(p.ProtocolError):
            await restarted.promote("Master", "https://other.example.com", "admin")

    async def test_explicit_reset_revokes_relationships_without_filesystem_changes(self):
        await self.follower()
        identifier, _, _, _ = await self.consumed()
        await self.store.activate(identifier, "master")
        original = self.store.node["node_id"]
        with self.assertRaises(p.ProtocolError):
            await self.store.reset("admin", "wrong")
        self.assertEqual((await self.store.relationship(identifier))["state"], "active")
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "original.mp3"
            file.write_bytes(b"media")
            await self.store.reset("admin", original)
            self.assertEqual(file.read_bytes(), b"media")
        self.assertEqual(self.store.node["role"], "Standalone")
        self.assertNotEqual(self.store.node["node_id"], original)
        self.assertEqual((await self.store.relationship(identifier))["state"], "revoked")

    async def test_follower_with_placed_media_cannot_revoke_upstream(self):
        await self.follower()
        identifier, _, _, _ = await self.consumed()
        await self.store.activate(identifier, "master")
        async with self.database.begin() as conn:
            await conn.execute(text("""
                INSERT INTO media_objects
                (media_id, object_kind, media_path, path_locator, created_at, updated_at)
                VALUES (:id, 'audio', 'music/shared/song.mp3', :locator, 'now', 'now')
            """), {"id": "a" * 64, "locator": "b" * 64})
        with self.assertRaises(p.ProtocolError):
            await self.store.revoke(identifier, "admin")
        self.assertEqual((await self.store.relationship(identifier))["state"], "active")

    async def test_pair_is_one_time_and_follower_accepts_only_one_master(self):
        await self.follower()
        a, credential, package, master = await self.consumed()
        with self.assertRaises(p.ProtocolError):
            await self.store.consume(package["payload"], uuid.uuid4().hex, peer(name="other"), credential)
        await self.store.activate(a, master["node_id"])
        await self.store.accept_mode(a, "Direct", master["node_id"])
        self.assertEqual((await self.store.relationship(a))["mode"], "Direct")
        with self.assertRaises(p.ProtocolError):
            await self.consumed(peer(name="other"))

    async def test_pair_expiration_and_wrong_owner_leave_no_relationship(self):
        await self.follower()
        package = (await self.store.create_pair("admin"))["payload"]
        with patch("app.services.federation.state.time.time", return_value=package["expires_at"]):
            with self.assertRaises(p.ProtocolError):
                await self.store.consume(package, uuid.uuid4().hex, peer(), secrets.token_urlsafe(48))
        package["node_id"] = uuid.uuid4().hex
        with self.assertRaises(p.ProtocolError):
            await self.store.consume(package, uuid.uuid4().hex, peer(), secrets.token_urlsafe(48))
        self.assertEqual(await self.store.list_relationships(), [])

    async def test_pending_cannot_authenticate_before_confirmation(self):
        await self.follower()
        identifier, credential, _, _ = await self.consumed()
        headers = {k.lower(): v for k, v in p.auth_headers(credential, identifier, "POST", "/internal/v1/confirm", b"{}").items()}
        with self.assertRaises(p.ProtocolError):
            await self.store.authenticate(headers, "POST", "/internal/v1/confirm", b"{}")
        await self.store.authenticate(headers, "POST", "/internal/v1/confirm", b"{}", allow_pending=True)
        await self.store.activate(identifier, "master")
        with self.assertRaises(p.ProtocolError):
            await self.store.authenticate(headers, "POST", "/internal/v1/confirm", b"{}", allow_pending=True)

    async def test_request_signature_binds_method_query_and_body(self):
        await self.follower()
        identifier, credential, _, _ = await self.consumed()
        await self.store.activate(identifier, "master")
        path = "/internal/v1/heartbeat?probe=2"
        headers = {k.lower(): v for k, v in p.auth_headers(credential, identifier, "GET", path).items()}
        for method, wrong_path, body in (("POST", path, b""), ("GET", path + "0", b""), ("GET", path, b"x")):
            with self.assertRaises(p.ProtocolError):
                await self.store.authenticate(headers, method, wrong_path, body)
        await self.store.authenticate(headers, "GET", path, b"")
        await self.store.revoke(identifier, "admin")
        new = {k.lower(): v for k, v in p.auth_headers(credential, identifier, "GET", path).items()}
        with self.assertRaises(p.ProtocolError):
            await self.store.authenticate(new, "GET", path, b"")

    async def test_future_timestamp_nonce_remains_used_at_window_boundary(self):
        await self.follower()
        identifier, credential, _, _ = await self.consumed()
        await self.store.activate(identifier, "master")
        now, path = int(time.time()), "/internal/v1/heartbeat"
        with patch("app.services.federation.protocol.time.time", return_value=now + 60):
            headers = {key.lower(): value for key, value in p.auth_headers(credential, identifier, "GET", path).items()}
        with patch("app.services.federation.state.time.time", return_value=now):
            await self.store.authenticate(headers, "GET", path, b"")
        with patch("app.services.federation.state.time.time", return_value=now + 120):
            with self.assertRaises(p.ProtocolError):
                await self.store.authenticate(headers, "GET", path, b"")

    async def test_state_and_audit_rollback_together(self):
        with patch.object(self.store, "log", new=AsyncMock(side_effect=RuntimeError("audit unavailable"))):
            with self.assertRaises(RuntimeError):
                await self.follower()
        restarted = State(self.database, self.key)
        await restarted.initialize()
        self.assertEqual(restarted.node["role"], "Standalone")

    async def test_authenticated_peer_revocation_needs_no_reverse_notification(self):
        from fastapi import FastAPI
        import httpx
        await self.follower()
        identifier, credential, _, _ = await self.consumed()
        await self.store.activate(identifier, "master")
        application = FastAPI()
        application.include_router(internal_nodes.router)
        path, body = "/internal/v1/revoke", b"{}"
        with patch.object(internal_nodes, "state", self.store), patch.object(internal_nodes.settings, "TLS_ENABLED", True), patch("app.services.media_catalog_cache.invalidate_media_catalog", new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="https://follower.example.com") as client:
                for _ in range(2):
                    response = await client.post(path, content=body, headers=p.auth_headers(credential, identifier, "POST", path, body))
                    self.assertEqual(response.status_code, 200)
                    row = await self.store.relationship(identifier)
                    self.assertEqual(row["state"], "revoked")
                    self.assertTrue(row["summary"].get("revocation_acknowledged"))

    async def test_repair_preserves_unacknowledged_revocation_credentials(self):
        await self.store.promote("Master", "https://master.example.com", "admin", 1024 ** 3)
        owner, old_id, new_id = peer("Follower"), uuid.uuid4().hex, uuid.uuid4().hex
        credential = secrets.token_urlsafe(48)
        await self.store.prepare(old_id, owner, credential, "admin")
        await self.store.activate(old_id, "admin")
        await self.store.revoke(old_id, "admin")
        with self.assertRaises(p.ProtocolError):
            await self.store.prepare(new_id, owner, secrets.token_urlsafe(48), "admin")
        old = await self.store.relationship(old_id)
        self.assertEqual(self.store.unseal(old["credential"]), credential)
        self.assertEqual(old["state"], "revoked")
        async with self.database.begin() as conn:
            await conn.execute(update(s.relationships).where(s.relationships.c.relationship_id == old_id)
                               .values(summary={"revocation_acknowledged": True}))
        await self.store.prepare(new_id, owner, secrets.token_urlsafe(48), "admin")
        self.assertEqual((await self.store.relationship(new_id))["state"], "pending")

    async def test_global_catalog_rejects_duplicate_logical_paths(self):
        from app.services import resource_pool
        await self.store.promote("Master", "https://master.example.com", "admin", 1024 ** 3)
        first = await resource_pool.reserve_upload(
            "music/same/song.wav", 4096, self.store.node["node_id"], self.database,
        )
        with self.assertRaises(p.ProtocolError):
            await resource_pool.reserve_upload(
                "music/same/song.wav", 4096, self.store.node["node_id"], self.database,
            )
        await resource_pool.finalize_upload(first["upload_id"], object_id=first["media_id"],
                                            actual_size=4096, etag='"test"', database=self.database)
        rows = await Catalog(self.store).resources(directory="music/same")
        self.assertEqual([(row["resource_id"], row["path"]) for row in rows],
                         [(first["media_id"], "music/same/song.wav")])

    async def test_business_backup_chunks_are_ordered_and_checksum_verified(self):
        from app.services import resource_pool
        await self.follower()
        now = int(time.time())
        async with self.database.begin() as conn:
            await conn.execute(insert(s.backup_members).values(
                member_id=self.store.node["node_id"], enabled=1, generation=0,
                last_success=0, lag_seconds=0, checksum="", state="pending", updated_at=now,
            ))
        master_id, generation = "a" * 32, 7
        payload = b"first-second"
        await resource_pool.backup_begin(master_id, generation, self.database)
        await resource_pool.backup_append(master_id, generation, 0, b"first-", self.database)
        await resource_pool.backup_append(master_id, generation, 1, b"second", self.database)
        with self.assertRaises(p.ProtocolError):
            await resource_pool.backup_commit(master_id, generation, "0" * 64,
                                              self.store.node, self.database)
        size = await resource_pool.backup_commit(master_id, generation,
            hashlib.sha256(payload).hexdigest(), self.store.node, self.database)
        self.assertEqual(size, len(payload))
        async with self.database.connect() as conn:
            row = (await conn.execute(select(s.business_backups).where(
                s.business_backups.c.master_id == master_id,
                s.business_backups.c.generation == generation))).mappings().one()
        self.assertEqual((row["state"], row["chunk_count"], row["size_bytes"]),
                         ("ready", 2, len(payload)))

    async def test_worker_lease_is_idempotent_and_rejects_stale_completion(self):
        from app.services import resource_pool
        key = hashlib.sha256(b"job").hexdigest()
        first = await resource_pool.enqueue_job("hash", {"object_id": "b" * 64}, key,
                                                member_id="c" * 32, database=self.database)
        second = await resource_pool.enqueue_job("hash", {"object_id": "b" * 64}, key,
                                                 member_id="c" * 32, database=self.database)
        self.assertEqual(first, second)
        leased = await resource_pool.lease_job("c" * 32, ["hash"], self.database)
        self.assertEqual(leased["job_id"], first)
        with self.assertRaises(p.ProtocolError):
            await resource_pool.complete_job("c" * 32, first, "wrong", {}, self.database)
        await resource_pool.complete_job("c" * 32, first, leased["lease"],
                                         {"sha256": "d" * 64}, self.database)
        self.assertIsNone(await resource_pool.lease_job("c" * 32, ["hash"], self.database))

    async def test_master_stats_are_single_authoritative_record(self):
        await self.store.promote("Master", "https://master.example.com", "admin", 1024 ** 3)
        identifier = uuid.uuid4().hex
        await self.store.prepare(identifier, peer("Follower", "follower"), secrets.token_urlsafe(48), "admin")
        await self.store.activate(identifier, "admin")
        relation = await self.store.relationship(identifier)
        resource = dict(resource_id="a" * 64, relationship_id=identifier, path="music/same/song.wav", payload=media_payload())
        async with self.database.begin() as conn:
            await conn.execute(insert(s.global_media).values(
                media_id=resource["resource_id"], storage_member_id=relation["peer_id"],
                object_id="b" * 64, media_path=resource["path"],
                path_locator=hashlib.sha256(resource["path"].encode()).hexdigest(),
                object_kind="audio", size_bytes=4096, etag='"1"', state="active",
                created_at=1, updated_at=1,
            ))
        resolve = AsyncMock(return_value=(resource, relation))
        with patch.object(routing, "state", self.store), patch.object(routing, "resolve", resolve):
            session = str(uuid.uuid4())
            results = await asyncio.gather(*(routing.mutate_stats(resource["resource_id"], resource["path"], session=session, played=30, duration=60) for _ in range(12)))
            self.assertEqual(sum(result["counted"] for result in results), 1)
            self.assertEqual(results[-1]["play_score"], 1)
            await routing.mutate_stats(resource["resource_id"], resource["path"], preference=500)
            item = {"resource_id": resource["resource_id"], "play_score": 999, "preference": -7}
            result = (await routing.attach_master_stats([item]))[0]
            self.assertEqual((result["play_score"], result["preference"]), (1, 500))

    async def test_lyrics_lookup_always_uses_master_business_data(self):
        from app.services import lyrics
        row = dict(resource_id="d" * 64, object_id="a" * 64,
                   path="music/same/song.wav", owner_id="b" * 32)
        relation = dict(relationship_id="c" * 32)
        entries = [{"time": 1, "text": "Master lyric"}]
        load = AsyncMock(return_value=("lyrics/master.lrc", entries))
        with patch.object(routing, "resolve", new=AsyncMock(return_value=(row, relation))), \
             patch.object(lyrics, "load_for_media", load), \
             patch.object(routing.runtime, "call", new=AsyncMock()) as call:
            self.assertEqual(await routing.lyric_entries("d" * 64, row["path"]), entries)
        load.assert_awaited_once_with(row["resource_id"])
        call.assert_not_awaited()

    async def test_standalone_does_not_open_transport_or_start_loop(self):
        runtime = Runtime()
        with patch("app.services.federation.runtime.state", self.store), patch("app.services.federation.runtime.transport.open") as opened:
            runtime.start()
            self.assertIsNone(runtime.task)
            opened.assert_not_called()

    async def test_runtime_has_no_legacy_catalog_scan_and_stops_cleanly(self):
        await self.follower()
        runtime = Runtime()
        heartbeat = asyncio.Event()

        async def tick(_relation):
            heartbeat.set()

        relation = {"state": "active", "relationship_id": "a" * 32}
        with patch("app.services.federation.runtime.state", self.store), \
             patch("app.services.federation.runtime.settings.TLS_ENABLED", True), \
             patch.object(self.store, "cleanup_playback_events", new=AsyncMock()), \
             patch.object(self.store, "list_relationships", new=AsyncMock(return_value=[relation])), \
             patch.object(runtime, "tick", new=AsyncMock(side_effect=tick)), \
             patch("app.services.federation.runtime.transport.open"), \
             patch("app.services.federation.runtime.transport.close", new=AsyncMock()):
            runtime.start()
            try:
                await asyncio.wait_for(heartbeat.wait(), 1)
            finally:
                await runtime.stop()
            self.assertIsNone(runtime.task)

    async def test_fixed_role_cannot_start_with_tls_disabled_or_reset_itself(self):
        await self.store.promote("Master", "https://master.example.com", "admin", 1024 ** 3)
        runtime = Runtime()
        with patch("app.services.federation.runtime.state", self.store), patch("app.services.federation.runtime.settings.TLS_ENABLED", False), patch("app.services.federation.runtime.transport.open") as opened:
            with self.assertRaises(p.ProtocolError):
                runtime.start()
            opened.assert_not_called()
        self.assertIsNone(runtime.task)
        self.assertEqual(self.store.node["role"], "Master")

    async def test_recovery_timestamp_survives_later_healthy_heartbeats(self):
        await self.follower()
        identifier, _, _, _ = await self.consumed()
        await self.store.activate(identifier, "master")
        now = int(time.time())
        with patch("app.services.federation.state.time.time", return_value=now):
            await self.store.heartbeat(identifier, True, 10, {"media_count": 2})
        with patch("app.services.federation.state.time.time", return_value=now + 30):
            await self.store.heartbeat(identifier, False)
        with patch("app.services.federation.state.time.time", return_value=now + 35):
            await self.store.heartbeat(identifier, True, 11, {"media_count": 3})
        with patch("app.services.federation.state.time.time", return_value=now + 60):
            await self.store.heartbeat(identifier, True, 12, {"media_count": 4})
        relation = await self.store.relationship(identifier)
        self.assertEqual(relation["summary"].get("recovered_at"), now + 35)
        self.assertEqual(relation["recoveries"], 1)
        self.assertEqual(relation["summary"]["media_count"], 4)

    async def test_incoming_heartbeat_cannot_mask_broken_peer_ingress(self):
        with patch.object(internal_nodes, "authenticated", new=AsyncMock()), patch.object(internal_nodes.catalog, "summary", new=AsyncMock(return_value={"protocol": 1})), patch.object(internal_nodes.state, "heartbeat", new=AsyncMock()) as recorded:
            self.assertEqual(await internal_nodes.heartbeat(None),
                             {"app_version": p.APP_VERSION, "protocol": p.PROTOCOL_VERSION})
            recorded.assert_not_awaited()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_media_entry_rejects_untrusted_http(self):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import Response
        from app.api.v1 import media
        application = FastAPI()
        application.include_router(media.router, prefix="/media")
        routed = AsyncMock(return_value=Response(status_code=307))
        lyrics = AsyncMock(return_value=[])
        with patch.object(routing, "stream", new=routed), patch.object(routing, "lyric_entries", new=lyrics), patch.object(internal_nodes.settings, "TLS_ENABLED", True):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://master.example.com") as client:
                paths = ["/media/stream?resource_id=" + "a" * 64] + ["/media/" + suffix + "?track=music/same/song.wav&resource_id=" + "a" * 64 for suffix in ("lyrics", "lyrics/content")]
                for path in paths:
                    response = await client.get(path, headers={"X-Forwarded-Proto": "https"})
                    self.assertEqual(response.status_code, 403)
                routed.assert_not_awaited()
                lyrics.assert_not_awaited()
                response = await client.get("https://master.example.com" + paths[0])
                self.assertEqual(response.status_code, 307)
        routed.assert_awaited_once()

    async def test_signed_incompatible_protocol_is_rejected(self):
        private, identifier = p.new_key(), uuid.uuid4().hex
        client = Transport()

        async def incompatible_identity(origin, path):
            return p.sign(private, {"node_id": identifier, "public_key": p.public_key(private),
                "role": "Follower", "endpoint": origin, "challenge": path.split("=", 1)[1],
                "protocol": p.PROTOCOL_VERSION + 1, "app_version": p.APP_VERSION})

        with patch.object(client, "request", new=AsyncMock(side_effect=incompatible_identity)):
            with self.assertRaises(p.ProtocolError):
                await client.identity("https://follower.example.com", expected_id=identifier,
                    expected_key=p.public_key(private), role="Follower")
        self.assertIsNone(client.client)


class ProtocolTests(unittest.TestCase):
    def test_mysql_schema_uses_collate_and_bounded_path_prefix_index(self):
        statements = list(s.migration_statements())
        self.assertTrue(all("COLLATE utf8mb4_bin" in statement for statement in statements))
        self.assertTrue(all("COLLATION=" not in statement for statement in statements))
        global_media = next(statement for statement in statements if "global_media_objects" in statement)
        self.assertIn("INDEX idx_global_media_path (media_path(191))", global_media)
        self.assertIn("CONSTRAINT uq_global_media_path UNIQUE (path_locator)", global_media)

    def test_media_token_binds_owner_resource_master_relation_and_expiry(self):
        credential = secrets.token_urlsafe(48)
        now = int(time.time())
        token = p.media_token(
            credential, "a" * 32, "b" * 32, "c" * 32, "d" * 64, "e" * 64, now,
        )
        payload = p.verify_media_token(credential, token, now)
        self.assertEqual(
            (payload["r"], payload["m"], payload["o"], payload["i"], payload["g"]),
            ("a" * 32, "b" * 32, "c" * 32, "d" * 64, "e" * 64),
        )
        for wrong_credential, wrong_token, stamp in ((secrets.token_urlsafe(48), token, now), (credential, token[:-1] + "!", now), (credential, token, now + p.TOKEN_SECONDS)):
            with self.assertRaises(p.ProtocolError):
                p.verify_media_token(wrong_credential, wrong_token, stamp)

    def test_identity_signature_rejects_substitution(self):
        private = p.new_key()
        signed = p.sign(private, {"node_id": "a" * 32})
        p.verify(p.public_key(private), signed)
        signed["payload"]["node_id"] = "b" * 32
        with self.assertRaises(p.ProtocolError):
            p.verify(p.public_key(private), signed)

    def test_https_endpoint_validation_does_not_downgrade(self):
        self.assertEqual(p.endpoint("https://Node.example.com:443/"), "https://node.example.com")
        for value in ("http://node.example.com", "https://user:pass@node.example.com", "https://node.example.com/x", "https://node.example.com?x", "https://169.254.169.254", "https://localhost", "https://127.0.0.1"):
            with self.subTest(value=value), self.assertRaises(p.ProtocolError):
                p.endpoint(value)

    def test_untrusted_forwarded_proto_cannot_authorize_node_control(self):
        request = Request({"type": "http", "scheme": "http", "path": "/internal/v1/identity", "query_string": b"", "server": ("node.example.com", 80), "client": ("203.0.113.4", 4567), "headers": [(b"x-forwarded-proto", b"https")]})
        with patch("app.api.internal_nodes.settings.TLS_ENABLED", True), self.assertRaises(Exception) as rejected:
            require_https(request)
        self.assertEqual(rejected.exception.status_code, 403)

    def test_global_media_path_rejects_roots_and_unsupported_attachments(self):
        from app.services.resource_pool import validate_media_path
        for path in ("/data/media/song.wav", "music/../song.wav", "lyrics/same.lrc", "music/same/song.lrc", "music/same/sub/deep/song.wav"):
            with self.subTest(path=path), self.assertRaises(p.ProtocolError):
                validate_media_path(path)

    def test_vault_key_is_persistent_and_temporary_files_are_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(vault_key(root), vault_key(root))
            self.assertEqual([file.name for file in root.iterdir()], ["node-vault.key"])


if __name__ == "__main__":
    unittest.main()
