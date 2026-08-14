import hashlib
import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.services import admin_service


class _FakeResult:
    def fetchall(self):
        return []


class _FakeConnection:
    async def execute(self, *_args, **_kwargs):
        return _FakeResult()


class _FakeTransaction:
    async def __aenter__(self):
        return _FakeConnection()

    async def __aexit__(self, *_args):
        return False


class _FakeEngine:
    def begin(self):
        return _FakeTransaction()


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    async def set(self, key, value, ex=None, **_kwargs):
        self.values[key] = str(value)
        self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.values.get(key)

    async def incr(self, key):
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def expire(self, key, seconds):
        if key not in self.values:
            return False
        self.ttls[key] = seconds
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return 1


class AdminTokenLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_token_uses_claim_window_then_switches_to_sliding_ttl(self):
        fake_redis = _FakeRedis()
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/v1/media/admin/elevate",
            "headers": [(b"user-agent", b"test")],
            "client": ("203.0.113.8", 12345),
        })
        token = "test-admin-token"
        token_key = admin_service.TOKEN_PREFIX + hashlib.sha256(token.encode()).hexdigest()
        fail_key = admin_service.FAIL_PREFIX + "203.0.113.8"

        with (
            patch.object(admin_service, "redis_client", fake_redis),
            patch.object(admin_service, "engine", _FakeEngine()),
            patch.object(admin_service.secrets, "token_urlsafe", return_value=token),
            patch.object(admin_service.settings, "ADMIN_TOKEN_INITIAL_TTL", 86400),
            patch.object(admin_service.settings, "ADMIN_TOKEN_TTL", 900),
        ):
            issued = await admin_service.issue_admin_token()
            self.assertEqual(issued, token)
            self.assertEqual(fake_redis.values[token_key], "pending")
            self.assertEqual(fake_redis.ttls[token_key], 86400)

            fake_redis.values[fail_key] = "3"
            token_hash = await admin_service.verify_admin_token(token, request)

        self.assertEqual(token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(fake_redis.values[token_key], "active")
        self.assertEqual(fake_redis.ttls[token_key], 900)
        self.assertNotIn(fail_key, fake_redis.values)


if __name__ == "__main__":
    unittest.main()
