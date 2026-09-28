"""Executable evidence for the September 2026 four-area audit."""
import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI, HTTPException, Request

from app.api.v1 import admin_transport, karaoke_users
from app.core.config import settings
from app.services import control_audit, karaoke_storage


def request():
    return Request({"type": "http", "method": "POST", "path": "/", "scheme": "https",
                    "headers": [], "client": ("203.0.113.1", 123), "server": ("test", 443), "query_string": b""})


class AuditRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_configured_while_offline_becomes_writable_on_recovery(self):
        from app.services import resource_pool
        current = {"storage_enabled": 1, "writable": 0, "allocated_bytes": 1024 ** 3,
                   "used_bytes": 100, "reserved_bytes": 50}
        result = MagicMock()
        result.mappings.return_value.first.return_value = current
        conn = MagicMock(execute=AsyncMock(return_value=result), scalar=AsyncMock(return_value=0))
        relation = {"peer_id": "peer", "relationship_id": "relation", "mode": "Direct",
                    "status": "online", "summary": {"storage_free": 1024 ** 3}}
        for enabled, status, expected in ((1, "online", 1), (1, "offline", 0), (0, "online", 0)):
            current["storage_enabled"] = enabled
            relation["status"] = status
            with self.subTest(enabled=enabled, status=status), patch.object(resource_pool, "_upsert", AsyncMock()) as upsert:
                await resource_pool.register_follower(relation, conn=conn)
            observed = upsert.call_args_list[0].args[2]
            self.assertEqual(observed["writable"], expected)
            self.assertEqual(observed["reserved_bytes"], 50)
            self.assertEqual(observed["used_bytes"], 100)

    async def test_unauthenticated_multipart_is_rejected_without_reading_body(self):
        from main import app
        from app.middleware import ip_security
        reads = []

        async def body():
            reads.append(True)
            yield b'--test\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n\r\nRIFF'
            yield b'\r\n--test--\r\n'

        with patch.object(ip_security, "get_ip_block", new=AsyncMock(return_value=None)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
                response = await client.post("/api/v1/media/admin/upload/item", content=body(),
                                             headers={"Content-Type": "multipart/form-data; boundary=test"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(reads, [])

    def test_loopback_proxy_does_not_make_remote_http_secure(self):
        scope = dict(request().scope, scheme="http", client=("127.0.0.1", 123),
                     headers=[(b"x-real-ip", b"203.0.113.1"), (b"x-forwarded-proto", b"http")])
        with patch.object(settings, "TRUSTED_PROXY_NETWORKS", "127.0.0.1/32"):
            self.assertFalse(admin_transport.secure_admin_transport(Request(scope)))

    async def test_invalid_new_password_is_client_error_and_audited(self):
        app = FastAPI()
        app.include_router(karaoke_users.router)
        audit = AsyncMock()
        with (patch.object(karaoke_users, "_user", new=AsyncMock(return_value={"user_id": "user", "password_hash": "hash"})),
              patch.object(karaoke_users.accounts, "verify_password", new=AsyncMock(return_value=True)),
              patch.object(karaoke_users.accounts, "audit", new=audit)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="https://test") as client:
                response = await client.post("/account/password", json={"current_password": "current", "new_password": "weak"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(audit.await_args.args[3], "failure")

    async def test_cancelled_control_action_records_interrupted_not_success(self):
        audit = AsyncMock()
        with patch.object(control_audit.admin_service, "audit", new=audit):
            with self.assertRaises(asyncio.CancelledError):
                async with control_audit.action("actor", "upgrade", request()):
                    raise asyncio.CancelledError()
        self.assertEqual([call.args[4] for call in audit.await_args_list], ["pending", "interrupted"])

    async def test_master_recording_stat_runs_off_event_loop(self):
        loop_thread = threading.get_ident()
        seen = []

        def stat(*args):
            seen.append(threading.get_ident())
            return {"size_bytes": 3}

        with (patch.object(karaoke_users, "_member_and_relation", new=AsyncMock(return_value=({"member_id": "m"}, None))),
              patch.object(karaoke_storage, "stat", new=stat)):
            result = await karaoke_users._recording_stat({"storage_member_id": "m", "user_id": "u", "recording_id": "r"})
        self.assertEqual(result["size_bytes"], 3)
        self.assertNotEqual(seen, [loop_thread])

    def test_malformed_optional_trailer_is_ignored(self):
        payloads = [b"{invalid", b"\xff", json.dumps({"version": 1, "lyrics": [{"time": float("inf"), "text": "x"}]}).encode()]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.bin"
            for payload in payloads:
                path.write_bytes(b"AUDIO" + payload + len(payload).to_bytes(8, "big") + karaoke_storage.TRAILER_MAGIC)
                with self.subTest(payload=payload):
                    self.assertEqual(karaoke_storage.parse_trailer(path), {})

    def test_cold_usage_cache_after_delete_counts_remaining_bytes_once(self):
        member, user = "a" * 32, "b" * 32
        with tempfile.TemporaryDirectory() as directory, patch.object(karaoke_storage, "ROOT", Path(directory)), patch.object(karaoke_storage, "_usage_cache", {}):
            first = karaoke_storage._path(member, user, "c" * 32)
            second = karaoke_storage._path(member, user, "d" * 32)
            first.parent.mkdir(parents=True)
            first.write_bytes(b"123")
            second.write_bytes(b"12345")
            self.assertEqual(karaoke_storage.remove(member, user, "c" * 32), 3)
            self.assertEqual(karaoke_storage.storage_usage(member), 5)

    def test_remove_user_invalidates_warm_and_cold_usage(self):
        member, first_user, second_user = "a" * 32, "b" * 32, "c" * 32
        for warm in (True, False):
            with tempfile.TemporaryDirectory() as directory, patch.object(karaoke_storage, "ROOT", Path(directory)), patch.object(karaoke_storage, "_usage_cache", {}):
                for user, payload in ((first_user, b"123"), (second_user, b"12345")):
                    path = karaoke_storage._path(member, user, "d" * 32)
                    path.parent.mkdir(parents=True)
                    path.write_bytes(payload)
                if warm:
                    self.assertEqual(karaoke_storage.storage_usage(member), 8)
                self.assertEqual(karaoke_storage.remove_user(member, first_user), 3)
                self.assertEqual(karaoke_storage.storage_usage(member), 5)

    async def test_upload_gate_rejects_csrf_without_body_and_allows_authorized_body(self):
        from app.middleware.admin_upload import AdminUploadMiddleware
        from app.services import admin_service
        for failure in (HTTPException(403, "CSRF"), None):
            reads, sent = [], []

            async def receive():
                reads.append(True)
                return {"type": "http.request", "body": b"audio", "more_body": False}

            async def downstream(scope, receive, send):
                await receive()
                await send({"type": "http.response.start", "status": 204, "headers": []})
                await send({"type": "http.response.body", "body": b""})

            async def send(message):
                sent.append(message)

            with patch.object(admin_service, "require_admin", new=AsyncMock(side_effect=failure, return_value="sid")):
                scope = dict(request().scope, path="/api/v1/media/admin/upload/lyric")
                await AdminUploadMiddleware(downstream)(scope, receive, send)
            self.assertEqual(sent[0]["status"], 403 if failure else 204)
            self.assertEqual(reads, [] if failure else [True])

    async def test_failed_audit_does_not_replace_original_control_failure(self):
        audit = AsyncMock(side_effect=[None, RuntimeError("audit unavailable")])
        with patch.object(control_audit.admin_service, "audit", new=audit):
            with self.assertRaisesRegex(ValueError, "original"):
                async with control_audit.action("actor", "upgrade", request()):
                    raise ValueError("original")

    async def test_follower_storage_hash_yields_and_preserves_digest(self):
        from app.api import internal_storage_integrity as storage
        from app.core.file_digest import file_digest
        entered, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        workers = []

        def slow_digest(path):
            workers.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return file_digest(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "music/album/song.wav"
            path.parent.mkdir(parents=True)
            payload = b"RIFF" + b"x" * (1024 * 1024 + 1)
            path.write_bytes(payload)
            req = request()
            req.state.node_control_body = json.dumps({"path": "music/album/song.wav"}).encode()
            with (patch.object(storage, "MEDIA_ROOT", root),
                  patch.dict(storage.state.node, role="Follower"),
                  patch.object(storage.legacy_internal, "authenticated", new=AsyncMock(return_value={"direction": "upstream"})),
                  patch.object(storage, "file_digest", new=slow_digest)):
                task = asyncio.create_task(storage.storage_stat(req, "a" * 64))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    self.assertFalse(task.done())
                finally:
                    release.set()
                result = await task
            self.assertNotEqual(workers, [loop_thread])
            self.assertEqual(result["size_bytes"], len(payload))
            self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
