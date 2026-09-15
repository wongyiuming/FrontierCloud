"""Capability provenance and pairing rejection before remote control I/O."""
import json
import secrets
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import Response
from sqlalchemy import update

from app.api import internal_nodes
from app.services.federation import protocol as p, schema as s
from app.services.federation.state import State
from tests.test_federation import Database, peer


class PairPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = Database()
        self.store = State(self.database, Fernet.generate_key())
        await self.store.initialize()
        await self.store.promote("Slave", "https://slave.example.com", "admin")
        self.application = FastAPI()
        self.application.include_router(internal_nodes.router)

    async def asyncTearDown(self):
        self.database.engine.dispose()

    async def package_body(self):
        package = await self.store.create_pair("admin")
        private = p.new_key()
        master = peer()
        master["public_key"] = p.public_key(private)
        body = {
            "package": package, "master": p.sign(private, master),
            "relationship_id": uuid.uuid4().hex, "credential": secrets.token_urlsafe(48),
        }
        return body, master

    async def test_expired_consumed_revoked_and_invalid_packages_do_not_probe(self):
        for status in ("expired", "consumed", "revoked", "wrong_token"):
            with self.subTest(status=status):
                body, _master = await self.package_body()
                changes = {"expires_at": int(time.time()) - 1} if status == "expired" else {
                    "token_hash": p.digest("unrelated-token")} if status == "wrong_token" else {"state": status}
                async with self.database.begin() as conn:
                    await conn.execute(update(s.pairs).where(
                        s.pairs.c.nonce == body["package"]["payload"]["nonce"]).values(**changes))
                probe = AsyncMock()
                with patch.object(internal_nodes, "state", self.store), patch.object(
                        internal_nodes.transport, "identity", probe), patch.object(internal_nodes.settings, "TLS_ENABLED", True):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.application),
                                                 base_url="https://slave.example.com") as client:
                        response = await client.post("/internal/v1/pair", json=body)
                self.assertEqual(response.status_code, 409)
                probe.assert_not_awaited()
                self.assertEqual(await self.store.list_relationships(), [])

    async def test_fresh_package_is_consumed_once_after_identity_verification(self):
        body, master = await self.package_body()
        probe = AsyncMock(return_value=master)
        with patch.object(internal_nodes, "state", self.store), patch.object(
                internal_nodes.transport, "identity", probe), patch.object(internal_nodes.settings, "TLS_ENABLED", True):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.application),
                                         base_url="https://slave.example.com") as client:
                first = await client.post("/internal/v1/pair", json=body)
                repeated = await client.post("/internal/v1/pair", json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(repeated.status_code, 409)
        probe.assert_awaited_once()
        self.assertEqual((await self.store.relationship(body["relationship_id"]))["state"], "pending")


class CapabilityAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_uses_signed_provenance_even_when_public_headers_disagree(self):
        credential = secrets.token_urlsafe(48)
        relation_id, master_id, owner_id, original = "a" * 32, "b" * 32, "c" * 32, "d" * 64
        origin_request_id, trace_id = "e" * 32, "f" * 32
        token = p.media_token(credential, relation_id, master_id, owner_id, original, int(time.time()),
                              request_id=origin_request_id, trace_id=trace_id)
        relation = {"state": "active", "direction": "upstream", "peer_id": master_id,
                    "peer_endpoint": "https://master.example.com", "credential": credential}
        store = SimpleNamespace(node={"role": "Slave", "node_id": owner_id}, unseal=lambda value: value,
                                relationship=AsyncMock(return_value=relation))
        scopes = []
        application = FastAPI()
        application.include_router(internal_nodes.router)

        async def record_scope(scope, receive, send):
            await application(scope, receive, send)
            scopes.append(scope.get("media_audit", {}))

        stream = AsyncMock(return_value=Response(headers={"X-Accel-Redirect": "/_protected_media/music/test/song.wav"}))
        with patch.object(internal_nodes, "state", store), patch.object(internal_nodes.settings, "TLS_ENABLED", True), patch.object(
                internal_nodes, "owned_path", AsyncMock(return_value="music/test/song.wav")), patch(
                "app.api.v1.media.stream_media_file", stream):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=record_scope),
                                         base_url="https://slave.example.com") as client:
                response = await client.get(f"/internal/v1/media/{original}", params={"token": token},
                                            headers={"X-Request-ID": "1" * 32, "Traceparent": "00-" + "2" * 32 + "-" + "3" * 16 + "-01"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(scopes[0]["parent_request_id"], origin_request_id)
        self.assertEqual(scopes[0]["trace_id"], trace_id)
        self.assertEqual(response.headers["X-Media-Resource-ID"], p.resource_id(owner_id, original))
        self.assertEqual(response.headers["X-Media-Object-ID"], original)
        self.assertEqual(response.headers["X-Media-Owner-ID"], owner_id)
        self.assertEqual(response.headers["X-Media-Parent-Request-ID"], origin_request_id)
        self.assertEqual(response.headers["X-Audit-Trace-ID"], trace_id)
        self.assertNotIn(token, json.dumps(scopes))

        with patch.object(internal_nodes, "state", store), patch.object(internal_nodes.settings, "TLS_ENABLED", True), patch.object(
                internal_nodes, "owned_path", AsyncMock(return_value="music/test/song.wav")), patch(
                "app.api.v1.media.stream_media_file", stream):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=record_scope),
                                         base_url="https://slave.example.com") as client:
                header_response = await client.head(f"/internal/v1/media/{original}",
                                                    headers={"X-Media-Capability": token})
                missing = await client.get(f"/internal/v1/media/{original}")
                conflicting = await client.get(f"/internal/v1/media/{original}", params={"token": token},
                                                headers={"X-Media-Capability": "different"})
        self.assertEqual(header_response.status_code, 200)
        self.assertEqual(header_response.headers["X-Media-Parent-Request-ID"], origin_request_id)
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(conflicting.status_code, 401)

    def test_legacy_capability_still_verifies_and_provenance_is_authenticated(self):
        credential, now = secrets.token_urlsafe(48), int(time.time())
        legacy = p.media_token(credential, "a" * 32, "b" * 32, "c" * 32, "d" * 64, now)
        self.assertNotIn("request_id", p.verify_media_token(credential, legacy, now))
        provenance = p.media_token(credential, "a" * 32, "b" * 32, "c" * 32, "d" * 64, now,
                                   request_id="e" * 32, trace_id="f" * 32)
        encoded, signature = provenance.split(".")
        value = json.loads(p.decode(encoded))
        value["request_id"] = "1" * 32
        forged = p.encode(p.canonical(value)) + "." + signature
        with self.assertRaises(p.ProtocolError):
            p.verify_media_token(credential, forged, now)
        self.assertEqual(p.verify_media_token(credential, provenance, now)["request_id"], "e" * 32)


if __name__ == "__main__":
    unittest.main()
