"""The effective ticket route bounds bytes before FastAPI JSON parsing."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, Request

from app.api.v1.endpoints import router
from app.api.v1 import karaoke_integrity, karaoke_users
from app.services import recording_crypto


class TicketBodyBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, chunks, content_length=None):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        headers = [(b"content-type", b"application/json")]
        if content_length is not None:
            headers.append((b"content-length", str(content_length).encode()))
        scope = {"type":"http", "asgi":{"version":"3.0"}, "http_version":"1.1",
            "method":"POST", "scheme":"https", "path":"/api/v1/karaoke/account/recordings/ticket",
            "raw_path":b"/api/v1/karaoke/account/recordings/ticket", "query_string":b"",
            "root_path":"", "headers":headers, "server":("4399.com",443), "client":("127.0.0.1",1234)}
        messages, reads = [], 0
        async def receive():
            nonlocal reads
            if reads >= len(chunks):
                raise AssertionError("Endpoint read beyond the supplied body")
            chunk = chunks[reads]
            reads += 1
            return {"type":"http.request", "body":chunk, "more_body":reads < len(chunks)}
        async def send(message):
            messages.append(message)
        await app(scope, receive, send)
        self.assertEqual(messages[0]["type"], "http.response.start")
        return messages[0]["status"], reads, b"".join(m.get("body",b"") for m in messages[1:])

    async def test_declared_oversize_never_reads_or_parses_or_authenticates(self):
        maximum = recording_crypto.MAX_METADATA_BYTES + 8192
        with patch.object(Request,"json",AsyncMock(side_effect=AssertionError("JSON parsed"))), \
             patch.object(karaoke_users,"_user",AsyncMock(side_effect=AssertionError("Auth reached"))):
            for value in (maximum + 1, "9" * 100, "0" * 5000 + str(maximum + 1)):
                status, reads, body = await self.request([b"unused"],value)
                self.assertEqual((status,reads),(413,0),body)

    async def test_chunked_and_forged_length_stop_at_actual_byte_bound(self):
        maximum = recording_crypto.MAX_METADATA_BYTES + 8192
        with patch.object(Request,"json",AsyncMock(side_effect=AssertionError("JSON parsed"))), \
             patch.object(karaoke_users,"_user",AsyncMock(side_effect=AssertionError("Auth reached"))):
            for declared in (None, 1, "invalid"):
                status, reads, body = await self.request([b"x" * maximum,b"y",b"never-read"],declared)
                self.assertEqual((status,reads),(413,2),body)

    async def test_normal_body_keeps_schema_and_authentication(self):
        async def deny(*args):
            raise HTTPException(401,"Authentication required")
        with patch.object(karaoke_users,"_user",AsyncMock(side_effect=deny)) as auth:
            status, _, _ = await self.request([b'{"size_bytes":10,"content_type":"audio/webm"}'])
            self.assertEqual(status,401)
            auth.assert_awaited_once()
            auth.reset_mock()
            status, _, _ = await self.request([b'{"size_bytes":0,"content_type":"audio/webm"}'])
            self.assertEqual(status,422)
            auth.assert_not_awaited()

    async def test_authenticated_ticket_uses_recovery_and_existing_handler(self):
        user = {"user_id":"1" * 32}
        with patch.object(karaoke_users,"_user",AsyncMock(return_value=user)), \
             patch.object(karaoke_integrity.deletion,"recover_deleting_recordings",AsyncMock()) as recover, \
             patch.object(karaoke_users,"create_ticket",AsyncMock(return_value={"status":"ok"})) as create:
            payload = json.dumps({"size_bytes":10,"content_type":"audio/webm"}).encode()
            status, reads, body = await self.request([payload[:10],payload[10:]],"0" * 5000 + str(len(payload)))
            self.assertEqual((status,reads),(200,2),body)
            recover.assert_awaited_once_with(user["user_id"])
            self.assertEqual(create.await_args.args[1].size_bytes,10)
