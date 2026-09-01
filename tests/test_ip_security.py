import inspect
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.api.v1 import admin
from app.core import client_ip as client_ip_module
from app.middleware import ip_security as middleware_module
from app.middleware.ip_security import IPSecurityMiddleware
from app.services import ip_security


def _scope(peer="198.51.100.8", headers=None, path="/missing"):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "https",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": (peer, 12345),
        "server": ("test", 443),
    }


async def _receive():
    return {"type": "http.disconnect"}


class ClientIPTrustTests(unittest.TestCase):
    def test_trusted_proxy_supplies_the_only_real_ip(self):
        identity = client_ip_module.resolve_client_identity(
            _scope("172.19.0.4", [(b"x-real-ip", b"203.0.113.9")])
        )
        self.assertEqual(identity.ip, "203.0.113.9")
        self.assertTrue(identity.from_trusted_proxy)

    def test_untrusted_peer_cannot_spoof_real_ip(self):
        identity = client_ip_module.resolve_client_identity(
            _scope("198.51.100.8", [(b"x-real-ip", b"203.0.113.9")])
        )
        self.assertEqual(identity.ip, "198.51.100.8")
        self.assertFalse(identity.from_trusted_proxy)

    def test_duplicate_proxy_header_is_rejected(self):
        identity = client_ip_module.resolve_client_identity(
            _scope(
                "172.19.0.4",
                [(b"x-real-ip", b"203.0.113.9"), (b"x-real-ip", b"198.51.100.4")],
            )
        )
        self.assertTrue(identity.trusted_proxy_header_missing)


class IPSecurityMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, downstream, scope=None, block=None):
        messages = []

        async def send(message):
            messages.append(message)

        with (
            patch.object(middleware_module, "get_ip_block", new=AsyncMock(return_value=block)),
            patch.object(middleware_module, "record_invalid_api", new=AsyncMock()) as record,
        ):
            await IPSecurityMiddleware(downstream)(scope or _scope(), _receive, send)
        return messages, record

    async def test_unknown_404_is_counted(self):
        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        _messages, record = await self._run(downstream)
        record.assert_awaited_once()

    async def test_matched_route_404_is_not_counted(self):
        async def downstream(scope, receive, send):
            scope["route"] = object()
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        _messages, record = await self._run(downstream)
        record.assert_not_awaited()

    async def test_active_ban_stops_request_before_backend(self):
        downstream = AsyncMock()
        messages, record = await self._run(
            downstream,
            block={"expires_at": "2030-01-01T00:00:00"},
        )
        downstream.assert_not_awaited()
        record.assert_not_awaited()
        start = next(message for message in messages if message["type"] == "http.response.start")
        self.assertEqual(start["status"], 403)

    async def test_trusted_proxy_without_real_ip_is_rejected(self):
        downstream = AsyncMock()
        messages, record = await self._run(downstream, scope=_scope("172.19.0.4"))
        downstream.assert_not_awaited()
        record.assert_not_awaited()
        start = next(message for message in messages if message["type"] == "http.response.start")
        self.assertEqual(start["status"], 400)


class FastAPIRouteClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def _request(self, path, query_string=b""):
        from main import app

        messages = []
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        scope = _scope("127.0.0.1", path=path)
        scope["query_string"] = query_string
        with (
            patch.object(middleware_module, "get_ip_block", new=AsyncMock(return_value=None)),
            patch.object(middleware_module, "record_invalid_api", new=AsyncMock()) as record,
        ):
            await app(scope, receive, send)
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        return status, record

    async def test_real_router_distinguishes_unknown_path_from_matched_404(self):
        unknown_status, unknown_record = await self._request("/etc/passwd")
        matched_status, matched_record = await self._request(
            "/api/v1/media/stream",
            b"file_path=missing%2Ftrack.mp3",
        )

        self.assertEqual(unknown_status, 404)
        unknown_record.assert_awaited_once()
        self.assertEqual(matched_status, 404)
        matched_record.assert_not_awaited()


class _FakeConnection:
    async def execute(self, *_args, **_kwargs):
        return None

    async def scalar(self, *_args, **_kwargs):
        return 0


class _FakeTransaction:
    async def __aenter__(self):
        return _FakeConnection()

    async def __aexit__(self, *_args):
        return False


class _FakeEngine:
    def begin(self):
        return _FakeTransaction()

    def connect(self):
        return _FakeTransaction()


class AutoBanThresholdTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixth_invalid_request_creates_24_hour_ban(self):
        now_ms = int(time.time() * 1000)
        fake_redis = AsyncMock()
        fake_redis.sismember.return_value = False
        fake_redis.eval.return_value = [6, now_ms - 1000]
        fake_redis.set.return_value = True

        with (
            patch.object(ip_security, "redis_client", fake_redis),
            patch.object(ip_security, "engine", _FakeEngine()),
            patch.object(ip_security.settings, "SECURITY_INVALID_API_LIMIT", 5),
        ):
            count = await ip_security.record_invalid_api(
                "203.0.113.10", "GET", "/etc/passwd", "scanner"
            )

        self.assertEqual(count, 6)
        self.assertEqual(fake_redis.set.await_args.kwargs["ex"], 86400)
        self.assertTrue(fake_redis.set.await_args.kwargs["nx"])
        fake_redis.zadd.assert_awaited_once()

    async def test_second_offense_creates_permanent_ban_without_redis_ttl(self):
        now_ms = int(time.time() * 1000)
        fake_redis = AsyncMock()
        fake_redis.sismember.return_value = False
        fake_redis.eval.return_value = [6, now_ms - 1000]
        fake_redis.set.return_value = True

        class PreviousBanConnection(_FakeConnection):
            async def scalar(self, *_args, **_kwargs):
                return 1
        class PreviousBanTransaction(_FakeTransaction):
            async def __aenter__(self): return PreviousBanConnection()
        class PreviousBanEngine(_FakeEngine):
            def connect(self): return PreviousBanTransaction()

        with patch.object(ip_security, "redis_client", fake_redis), patch.object(ip_security, "engine", PreviousBanEngine()), \
             patch.object(ip_security.settings, "SECURITY_INVALID_API_LIMIT", 5):
            count = await ip_security.record_invalid_api("203.0.113.11", "GET", "/second", "scanner")

        self.assertEqual(count, 6)
        self.assertNotIn("ex", fake_redis.set.await_args.kwargs)
        self.assertTrue(fake_redis.set.await_args.kwargs["nx"])

    def test_ban_runtime_never_deletes_audit_history(self):
        record_source = inspect.getsource(ip_security.record_invalid_api)
        list_source = inspect.getsource(ip_security.list_security_history)
        self.assertNotIn("DELETE FROM ip_auto_ban_events", record_source)
        self.assertNotIn("DELETE FROM ip_auto_ban_events", list_source)
        self.assertIn("SET status='expired'", list_source)

    def test_history_query_supports_filters_and_bounded_pagination(self):
        source = inspect.getsource(ip_security.list_security_history)
        self.assertIn("ip_address = :ip", source)
        self.assertIn("status = :status", source)
        self.assertIn("LIMIT :limit OFFSET :offset", source)

    def test_manual_reban_creates_a_new_audit_event(self):
        source = inspect.getsource(ip_security.manual_ban_ip)
        self.assertIn("INSERT INTO ip_auto_ban_events", source)
        self.assertIn("'manual'", source)
        self.assertIn("created_by_session_hash", source)
        self.assertNotIn("UPDATE ip_auto_ban_events", source)


class AdminAuthenticationCoverageTests(unittest.TestCase):
    def test_every_admin_view_route_except_elevation_requires_session(self):
        bootstrap = {"/elevate"}
        missing = []
        for route in admin.router.routes:
            if route.path in bootstrap:
                continue
            dependencies = {dependency.call for dependency in route.dependant.dependencies}
            if admin.require_session not in dependencies:
                missing.append(route.path)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
