import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import Response

from app.services import admin_service


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v1/media/admin/elevate",
        "headers": [],
        "client": ("203.0.113.8", 12345),
    })


class _Pipeline:
    def __init__(self, fail=False):
        self.commands = []
        self.fail = fail

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.commands.append((name, args, kwargs))
            return self
        return record

    async def execute(self):
        if self.fail:
            raise RedisError("pipeline failed")
        return [1 if name != "expire" else True for name, _args, _kwargs in self.commands]


class _Redis:
    def __init__(self):
        self.values = {}
        self.ttls = {}
        self.eval_calls = []
        self.pipelines = []
        self.sessions = {}

    async def eval(self, script, number_of_keys, key, window, increment):
        self.eval_calls.append((script, number_of_keys, key, window, increment))
        count = int(self.values.get(key, 0))
        if int(increment) == 1:
            count += 1
            self.values[key] = str(count)
        if count > 0 and self.ttls.get(key, -1) < 0:
            self.ttls[key] = int(window)
        return count

    async def delete(self, key):
        self.values.pop(key, None)
        return 1

    def pipeline(self, transaction):
        pipeline = _Pipeline()
        pipeline.transaction = transaction
        self.pipelines.append(pipeline)
        return pipeline

    async def scan_iter(self, match=None):
        for key in list(self.sessions):
            yield key


class AdminRedisTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_counter_lua_repairs_a_missing_ttl_atomically(self):
        fake = _Redis()
        redis_key = admin_service.FAIL_PREFIX + "203.0.113.8"
        fake.values[redis_key] = "2"
        fake.ttls[redis_key] = -1
        with patch.object(admin_service, "redis_client", fake):
            value = await admin_service._failed_attempt_count(redis_key, increment=False)

        self.assertEqual(value, 2)
        self.assertEqual(fake.ttls[redis_key], admin_service.settings.ADMIN_FAILED_WINDOW)
        script = fake.eval_calls[0][0]
        self.assertIn("INCR", script)
        self.assertIn("TTL", script)
        self.assertIn("EXPIRE", script)

    async def test_session_hash_and_ttl_are_written_in_one_pipeline(self):
        fake = _Redis()
        response = Response()
        with (
            patch.object(admin_service, "redis_client", fake),
            patch.object(
                admin_service.secrets,
                "token_urlsafe",
                side_effect=["session-secret", "csrf-secret"],
            ),
        ):
            await admin_service.create_session("key-hash", _request(), response)

        self.assertEqual(len(fake.pipelines), 1)
        self.assertTrue(fake.pipelines[0].transaction)
        names = [name for name, _args, _kwargs in fake.pipelines[0].commands]
        self.assertEqual(names, ["hset", "expire"])

    async def test_rotation_returns_published_key_when_redis_reconciliation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text("old-admin-key-123456789\n", encoding="utf-8")
            new_key = "new-admin-key-123456789"
            with (
                patch.object(admin_service, "ADMIN_KEY_FILE", key_file),
                patch.object(admin_service.secrets, "token_urlsafe", return_value=new_key),
                patch.object(
                    admin_service,
                    "_replace_admin_sessions",
                    new=AsyncMock(side_effect=RedisError("down")),
                ),
                patch.object(admin_service.logger, "exception") as logged,
            ):
                returned = await admin_service.rotate_admin_key("current", None, None)

            self.assertEqual(returned, new_key)
            self.assertEqual(key_file.read_text(encoding="utf-8").strip(), new_key)
            self.assertEqual(list(Path(directory).glob(".*.new")), [])
            logged.assert_called_once()

    async def test_concurrent_rotations_do_not_share_a_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text("old-admin-key-123456789\n", encoding="utf-8")
            generated = ["random-admin-key-a-123456789", "random-admin-key-b-123456789"]
            with (
                patch.object(admin_service, "ADMIN_KEY_FILE", key_file),
                patch.object(admin_service.secrets, "token_urlsafe", side_effect=generated),
                patch.object(admin_service, "_replace_admin_sessions", new=AsyncMock()),
            ):
                results = await asyncio.gather(
                    admin_service.rotate_admin_key("one", None, None),
                    admin_service.rotate_admin_key("two", None, None),
                )

            self.assertEqual(results, generated)
            self.assertEqual(key_file.read_text(encoding="utf-8").strip(), generated[-1])
            self.assertEqual(list(Path(directory).glob(".*.new")), [])
            self.assertEqual(
                hashlib.sha256(results[-1].encode()).hexdigest(),
                admin_service._hash(generated[-1]),
            )


class _FailingAuditConnection:
    async def execute(self, *_args, **_kwargs):
        raise SQLAlchemyError("audit unavailable")


class _FailingAuditContext:
    async def __aenter__(self):
        return _FailingAuditConnection()

    async def __aexit__(self, *_args):
        return False


class _FailingAuditEngine:
    def begin(self):
        return _FailingAuditContext()


class AdminAuditTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_outage_does_not_reverse_an_already_committed_action(self):
        with (
            patch.object(admin_service, "engine", _FailingAuditEngine()),
            patch.object(admin_service.logger, "exception") as logged,
        ):
            await admin_service.audit(
                "session",
                "delete",
                1,
                "music/a",
                "success",
                "deleted=1",
                _request(),
            )
        logged.assert_called_once()


if __name__ == "__main__":
    unittest.main()
