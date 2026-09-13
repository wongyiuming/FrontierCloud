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
from sqlalchemy import create_engine, insert, select, update
from starlette.requests import Request

from app.api.internal_nodes import require_https
from app.services.federation import protocol as p, schema as s, routing
from app.services.federation.catalog import Catalog, valid_payload
from app.services.federation.state import State, vault_key
from app.services.federation.runtime import Runtime


class Connection:
    def __init__(self, connection):
        self.connection = connection

    async def execute(self, statement, parameters=None):
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
        self.database = Database()
        self.key = Fernet.generate_key()
        self.store = State(self.database, self.key)
        await self.store.initialize()

    async def asyncTearDown(self):
        self.database.engine.dispose()

    async def slave(self):
        await self.store.promote("Slave", "https://slave.example.com", "admin")

    async def consumed(self, master=None):
        master = master or peer()
        package = await self.store.create_pair("admin")
        identifier, credential = uuid.uuid4().hex, secrets.token_urlsafe(48)
        await self.store.consume(package["payload"], identifier, master, credential)
        return identifier, credential, package, master

    async def test_promotion_persists_and_cannot_change_online(self):
        original = self.store.node["node_id"]
        await self.slave()
        restarted = State(self.database, self.key)
        await restarted.initialize()
        self.assertEqual(restarted.node["role"], "Slave")
        self.assertEqual(restarted.node["node_id"], original)
        with self.assertRaises(p.ProtocolError):
            await restarted.promote("Master", "https://other.example.com", "admin")

    async def test_explicit_reset_revokes_relationships_without_filesystem_changes(self):
        await self.slave()
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

    async def test_pair_is_one_time_and_multi_master_revoke_is_independent(self):
        await self.slave()
        a, credential, package, master = await self.consumed()
        with self.assertRaises(p.ProtocolError):
            await self.store.consume(package["payload"], uuid.uuid4().hex, peer(name="other"), credential)
        b, other_credential, _, _ = await self.consumed(peer(name="other"))
        self.assertNotEqual(credential, other_credential)
        await self.store.activate(a, master["node_id"])
        await self.store.activate(b, "other")
        await self.store.revoke(a, "admin")
        self.assertEqual((await self.store.relationship(b))["state"], "active")

    async def test_pair_expiration_and_wrong_owner_leave_no_relationship(self):
        await self.slave()
        package = (await self.store.create_pair("admin"))["payload"]
        with patch("app.services.federation.state.time.time", return_value=package["expires_at"]):
            with self.assertRaises(p.ProtocolError):
                await self.store.consume(package, uuid.uuid4().hex, peer(), secrets.token_urlsafe(48))
        package["node_id"] = uuid.uuid4().hex
        with self.assertRaises(p.ProtocolError):
            await self.store.consume(package, uuid.uuid4().hex, peer(), secrets.token_urlsafe(48))
        self.assertEqual(await self.store.list_relationships(), [])

    async def test_pending_cannot_authenticate_before_confirmation(self):
        await self.slave()
        identifier, credential, _, _ = await self.consumed()
        headers = {k.lower(): v for k, v in p.auth_headers(credential, identifier, "POST", "/internal/v1/confirm", b"{}").items()}
        with self.assertRaises(p.ProtocolError):
            await self.store.authenticate(headers, "POST", "/internal/v1/confirm", b"{}")
        await self.store.authenticate(headers, "POST", "/internal/v1/confirm", b"{}", allow_pending=True)
        await self.store.activate(identifier, "master")
        with self.assertRaises(p.ProtocolError):
            await self.store.authenticate(headers, "POST", "/internal/v1/confirm", b"{}", allow_pending=True)

    async def test_request_signature_binds_method_query_and_body(self):
        await self.slave()
        identifier, credential, _, _ = await self.consumed()
        await self.store.activate(identifier, "master")
        path = "/internal/v1/catalog?cursor=2"
        headers = {k.lower(): v for k, v in p.auth_headers(credential, identifier, "GET", path).items()}
        for method, wrong_path, body in (("POST", path, b""), ("GET", path + "0", b""), ("GET", path, b"x")):
            with self.assertRaises(p.ProtocolError):
                await self.store.authenticate(headers, method, wrong_path, body)
        await self.store.authenticate(headers, "GET", path, b"")
        await self.store.revoke(identifier, "admin")
        new = {k.lower(): v for k, v in p.auth_headers(credential, identifier, "GET", path).items()}
        with self.assertRaises(p.ProtocolError):
            await self.store.authenticate(new, "GET", path, b"")

    async def test_state_and_audit_rollback_together(self):
        with patch.object(self.store, "log", new=AsyncMock(side_effect=RuntimeError("audit unavailable"))):
            with self.assertRaises(RuntimeError):
                await self.slave()
        restarted = State(self.database, self.key)
        await restarted.initialize()
        self.assertEqual(restarted.node["role"], "Standalone")

    async def test_catalog_distinguishes_same_path_and_rejects_stale_cursor(self):
        await self.store.promote("Master", "https://master.example.com", "admin")
        relations = []
        for name in ("one", "two"):
            owner = peer("Slave", name)
            identifier = uuid.uuid4().hex
            await self.store.prepare(identifier, owner, secrets.token_urlsafe(48), "admin")
            await self.store.activate(identifier, "admin")
            relations.append(await self.store.relationship(identifier))
        catalog = Catalog(self.store)
        original = hashlib.sha256(b"legacy-same-path").hexdigest()
        for relation in relations:
            page = {"owner_id": relation["peer_id"], "head": 1, "cursor": 1, "complete": True,
                "items": [{"object_id": original, "version": 1, "deleted": False, "payload": media_payload()}]}
            await catalog.apply(relation, page, 0)
            with self.assertRaises(p.ProtocolError):
                await catalog.apply(relation, page, 0)
        rows = await catalog.resources(directory="music/same")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["resource_id"], rows[1]["resource_id"])
        self.assertEqual(rows[0]["path"], rows[1]["path"])
        relation = relations[0]
        deleted = {"owner_id": relation["peer_id"], "head": 2, "cursor": 2, "complete": True,
            "items": [{"object_id": original, "version": 2, "deleted": True, "payload": media_payload()}]}
        await catalog.apply(relation, deleted, 1)
        self.assertEqual(len(await catalog.resources()), 1)

    async def test_catalog_invalid_page_rolls_back_items_and_cursor(self):
        await self.store.promote("Master", "https://master.example.com", "admin")
        identifier = uuid.uuid4().hex
        await self.store.prepare(identifier, peer("Slave", "slave"), secrets.token_urlsafe(48), "admin")
        await self.store.activate(identifier, "admin")
        relation = await self.store.relationship(identifier)
        page = {"owner_id": relation["peer_id"], "head": 2, "cursor": 2, "complete": True,
            "items": [{"object_id": "a" * 64, "version": 1, "deleted": False, "payload": media_payload()},
                      {"object_id": "b" * 64, "version": 2, "deleted": False, "payload": media_payload("/data/media/music/song.wav")}]}
        with self.assertRaises(p.ProtocolError):
            await Catalog(self.store).apply(relation, page, 0)
        self.assertEqual((await self.store.relationship(identifier))["cursor"], 0)
        self.assertEqual(await Catalog(self.store).resources(), [])

    async def test_master_stats_fallback_then_concurrent_master_updates_are_authoritative(self):
        await self.store.promote("Master", "https://master.example.com", "admin")
        identifier = uuid.uuid4().hex
        await self.store.prepare(identifier, peer("Slave", "slave"), secrets.token_urlsafe(48), "admin")
        await self.store.activate(identifier, "admin")
        relation = await self.store.relationship(identifier)
        resource = dict(resource_id="a" * 64, relationship_id=identifier, path="music/same/song.wav", payload=media_payload())
        resolve = AsyncMock(return_value=(resource, relation))
        with patch.object(routing, "state", self.store), patch.object(routing, "resolve", resolve):
            session = str(uuid.uuid4())
            results = await asyncio.gather(*(routing.mutate_stats(resource["resource_id"], resource["path"], session=session, played=30, duration=60) for _ in range(12)))
            self.assertEqual(sum(result["counted"] for result in results), 1)
            self.assertEqual(results[-1]["play_score"], 8)
            await asyncio.gather(*(routing.mutate_stats(resource["resource_id"], resource["path"], delta=1) for _ in range(12)))
            item = {"resource_id": resource["resource_id"], "play_score": 999, "preference": -2}
            result = (await routing.attach_master_stats([item]))[0]
            self.assertEqual((result["play_score"], result["preference"]), (8, 7))

    async def test_lyrics_lookup_stays_on_the_resolved_owner(self):
        row = dict(object_id="a" * 64, path="music/same/song.wav", owner_id="b" * 32)
        relation = dict(relationship_id="c" * 32)
        entries = [{"time": 1, "text": "Slave's lyric"}]
        call = AsyncMock(return_value={"entries": entries})
        with patch.object(routing, "resolve", new=AsyncMock(return_value=(row, relation))), patch.object(routing.runtime, "call", call):
            self.assertEqual(await routing.lyric_entries("d" * 64, row["path"]), entries)
        call.assert_awaited_once_with(relation, "/internal/v1/lyrics/" + row["object_id"])

    async def test_standalone_does_not_open_transport_or_start_loop(self):
        runtime = Runtime()
        with patch("app.services.federation.runtime.state", self.store), patch("app.services.federation.runtime.transport.open") as opened:
            runtime.start()
            self.assertIsNone(runtime.task)
            opened.assert_not_called()


class ProtocolTests(unittest.TestCase):
    def test_media_token_binds_owner_resource_master_relation_and_expiry(self):
        credential = secrets.token_urlsafe(48)
        now = int(time.time())
        token = p.media_token(credential, "a" * 32, "b" * 32, "c" * 32, "d" * 64, now)
        payload = p.verify_media_token(credential, token, now)
        self.assertEqual((payload["r"], payload["m"], payload["o"], payload["i"]), ("a" * 32, "b" * 32, "c" * 32, "d" * 64))
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

    def test_catalog_rejects_global_roots_and_unsupported_attachments(self):
        for path in ("/data/media/song.wav", "music/../song.wav", "lyrics/same.lrc", "music/same/song.lrc", "music/same/sub/deep/song.wav"):
            with self.subTest(path=path), self.assertRaises(p.ProtocolError):
                valid_payload(media_payload(path))

    def test_vault_key_is_persistent_and_temporary_files_are_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(vault_key(root), vault_key(root))
            self.assertEqual([file.name for file in root.iterdir()], ["node-vault.key"])


if __name__ == "__main__":
    unittest.main()
