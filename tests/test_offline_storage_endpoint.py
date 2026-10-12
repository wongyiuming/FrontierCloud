import asyncio
import base64
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import serialization
from sqlalchemy import select, text, update

from app.services import offline_storage_endpoint as offline
from app.services.federation import protocol as p, schema as s
from app.services.federation.transport import Transport


@unittest.skipIf(os.name == "nt", "storage writer/recovery fixtures require Linux flock")
class OfflineEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.test_upload_recovery_runtime import UploadRecoveryRuntime
        self.fixture = UploadRecoveryRuntime()
        await self.fixture.asyncSetUp()
        self.master, self.follower = self.fixture.master, self.fixture.follower

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def facts(self, store):
        from app.core import db
        async with store.database.connect() as conn:
            return {name: (await conn.execute(text('SELECT * FROM "' + name + '"'))).all()
                    for name in sorted(await db._table_names(conn))}

    async def test_follower_move_preserves_every_identity_relationship_and_business_fact(self):
        previous = self.follower.node["endpoint"]
        endpoint = "https://new.storage.4399.com"
        before = await self.facts(self.follower)
        await offline.move(self.follower, self.follower.node["node_id"], previous, endpoint)
        after = await self.facts(self.follower)
        self.assertEqual({k: v for k, v in before.items() if k not in {"node_identity", "node_audit"}}, {k: v for k, v in after.items() if k not in {"node_identity", "node_audit"}})
        async with self.follower.database.connect() as conn:
            identity = dict((await conn.execute(select(s.identity))).mappings().one())
        self.assertEqual(identity, {**self.follower.node, "endpoint": endpoint})
        await offline.move(self.follower, self.follower.node["node_id"], previous, endpoint)
        self.assertEqual(await self.facts(self.follower), after)

    async def test_wrong_identity_previous_and_unfinished_work_never_mutate(self):
        before = await self.facts(self.follower)
        for node, previous in (("a" * 32, self.follower.node["endpoint"]), (self.follower.node["node_id"], "https://wrong.4399.com")):
            with self.assertRaises(p.ProtocolError):
                await offline.move(self.follower, node, previous, "https://new.storage.4399.com")
            self.assertEqual(await self.facts(self.follower), before)
        from app.api import internal_storage_integrity as storage
        await storage._reserve("a" * 64, self.fixture.path, len(self.fixture.data))
        before = await self.facts(self.follower)
        with self.assertRaisesRegex(p.ProtocolError, "converged"):
            await offline.move(self.follower, self.follower.node["node_id"], self.follower.node["endpoint"], "https://new.storage.4399.com")
        self.assertEqual(await self.facts(self.follower), before)

    async def test_committed_publication_journal_blocks_handover_when_sql_is_idle(self):
        from app.services import media_publication_recovery as publication
        root = self.fixture.master_root
        payload = b"ID3-committed-publication"
        staged = root / ".publication.part"
        staged.write_bytes(payload)
        value = publication.create(root, "music/artist/pending.mp3", staged, payload, None)
        target = root / value["path"]
        target.parent.mkdir(parents=True)
        staged.replace(target)
        await publication.register(self.master.database, value)
        # This is precisely commit-before-journal.finish: SQL alone is idle.
        async with self.master.database.connect() as conn:
            await offline.idle(conn)
        before = await self.facts(self.master)
        key_directory = root.parent / "secrets"
        key_directory.mkdir(exist_ok=True)
        key = base64.urlsafe_b64encode(self.master._vault._signing_key + self.master._vault._encryption_key)
        (key_directory / "node-vault.key").write_bytes(key)
        os.chmod(key_directory / "node-vault.key", 0o600)
        (root.parent / ".frontiercloud-native-maintenance").write_bytes(b"operator")
        relation = await self.master.relationship(self.fixture.relationship)
        options = SimpleNamespace(command="storage-rebind", confirm_node_id=self.master.node["node_id"],
            previous_endpoint=relation["peer_endpoint"], endpoint="https://new.storage.4399.com", relationship=self.fixture.relationship)
        probe = AsyncMock(side_effect=AssertionError("Pending publication must block before remote identity probe"))
        with (patch.object(offline.db, "engine", self.master.database),
              patch.object(offline, "SECRET_DIR", key_directory),
              patch.object(offline.runtime_gate, "DATA_ROOT", root.parent),
              patch.object(offline.settings, "SQLITE_PATH", str(root.parent / "state.db")),
              patch.object(offline.Transport, "identity", new=probe)):
            with self.assertRaisesRegex(p.ProtocolError, "Pending media publication"):
                await offline.execute(options)
        probe.assert_not_awaited()
        self.assertEqual(await self.facts(self.master), before)
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(publication.pending(root), [publication.journal_path(root, value)])

    async def test_master_rebind_compares_key_and_credential_after_probe(self):
        relation = await self.master.relationship(self.fixture.relationship)
        endpoint = "https://new.storage.4399.com"
        calls = []
        class Prover:
            async def identity(prover, target, **expected):
                calls.append(expected)
                async with self.master.database.begin() as conn:
                    await conn.execute(update(s.relationships).where(s.relationships.c.relationship_id == self.fixture.relationship).values(credential=self.master.seal("changed")))
                return {"node_id": relation["peer_id"], "public_key": relation["peer_key"], "role": "Follower", "endpoint": endpoint}
        with self.assertRaisesRegex(p.ProtocolError, "changed during"):
            await offline.rebind(self.master, Prover(), self.master.node["node_id"], self.fixture.relationship, relation["peer_endpoint"], endpoint)
        current = await self.master.relationship(self.fixture.relationship)
        self.assertEqual(current["peer_endpoint"], relation["peer_endpoint"])
        self.assertEqual(calls, [{"expected_id": relation["peer_id"], "expected_key": relation["peer_key"], "role": "Follower"}])

    async def test_master_uses_real_certificate_verified_tls_and_signed_identity(self):
        from tests.certificate_fixtures import TestCA
        ca = TestCA()
        host = "new.storage.4399.com"
        certificate, key = ca.pair(host)
        root = self.fixture.root / "tls"
        root.mkdir()
        (root / "certificate.pem").write_bytes(certificate)
        (root / "private.pem").write_bytes(key)
        node = self.follower.node
        private = self.follower.unseal(node["private_key"])
        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                challenge = parse_qs(urlsplit(handler.path).query)["challenge"][0]
                body = p.canonical(p.sign(handler.server.signer, {"node_id": node["node_id"], "role": "Follower", "endpoint": handler.server.endpoint,
                    "public_key": p.public_key(private), "challenge": challenge, "protocol": p.PROTOCOL_VERSION, "app_version": p.APP_VERSION}))
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
            def log_message(handler, *_args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.endpoint = f"https://{host}:{server.server_port}"
        server.signer = private
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(root / "certificate.pem", root / "private.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        trusted = ssl.create_default_context(cadata=ca.root.public_bytes(serialization.Encoding.PEM).decode())
        transport = Transport()
        lookup = socket.getaddrinfo
        def local_dns(value, port, *args, **kwargs):
            return lookup("127.0.0.1" if value in (host, host.encode()) else value, port, *args, **kwargs)
        try:
            relation = await self.master.relationship(self.fixture.relationship)
            with patch("app.services.federation.transport.ssl.create_default_context", return_value=trusted):
                transport.open()
            self.assertEqual(trusted.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(trusted.check_hostname)
            with patch.object(socket, "getaddrinfo", new=local_dns):
                await offline.rebind(self.master, transport, self.master.node["node_id"], self.fixture.relationship, relation["peer_endpoint"], server.endpoint)
                current = await self.master.relationship(self.fixture.relationship)
                self.assertEqual(current["peer_endpoint"], server.endpoint)
                self.assertEqual(current["credential"], relation["credential"])
                self.assertEqual(current["peer_key"], relation["peer_key"])
                self.assertEqual(current["status"], "offline")
                self.assertEqual(current["last_heartbeat"], 0)
                before = await self.facts(self.master)
                await offline.rebind(self.master, transport, self.master.node["node_id"], self.fixture.relationship, relation["peer_endpoint"], server.endpoint)
                self.assertEqual(await self.facts(self.master), before)
                server.signer = p.new_key()
                with self.assertRaises(p.ProtocolError):
                    await offline.rebind(self.master, transport, self.master.node["node_id"], self.fixture.relationship, relation["peer_endpoint"], server.endpoint)
                self.assertEqual(await self.facts(self.master), before)
        finally:
            await transport.close()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
