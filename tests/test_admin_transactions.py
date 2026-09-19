import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from fastapi import HTTPException
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

    async def eval(self, script, number_of_keys, *arguments):
        self.eval_calls.append((script, number_of_keys, *arguments))
        if script == admin_service._REDEEM_TEMPORARY_KEY_SCRIPT:
            return self.values.pop(arguments[0], None)
        key, window, increment = arguments
        count = int(self.values.get(key, 0))
        if int(increment) == 1:
            count += 1
            self.values[key] = str(count)
        if count > 0 and self.ttls.get(key, -1) < 0:
            self.ttls[key] = int(window)
        return count

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        return 1

    def pipeline(self, transaction):
        pipeline = _Pipeline()
        pipeline.transaction = transaction
        self.pipelines.append(pipeline)
        return pipeline

    def lock(self, name, **kwargs):
        self.lock_name = name
        self.lock_options = kwargs
        return _Lock()

    async def scan_iter(self, match=None):
        for key in list(self.sessions):
            yield key


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class AdminRedisTransactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        audit = patch.object(admin_service, "audit", new=AsyncMock())
        self.audit = audit.start()
        self.addCleanup(audit.stop)

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

    async def test_temporary_key_is_consumed_once_and_creates_its_own_sliding_window(self):
        fake = _Redis()
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text("stable-admin-key-123456789\n", encoding="utf-8")
            with (
                patch.object(admin_service, "ADMIN_KEY_FILE", key_file),
                patch.object(admin_service, "redis_client", fake),
                patch.object(admin_service.secrets, "token_urlsafe", return_value="one-time-key"),
            ):
                key = await admin_service.issue_temporary_admin_key("issuer", 15)
                credential = await admin_service.redeem_admin_credential(key, _request())
                response = Response()
                with patch.object(
                    admin_service.secrets,
                    "token_urlsafe",
                    side_effect=["temporary-session", "temporary-csrf"],
                ):
                    await admin_service.create_session(
                        credential.key_hash,
                        _request(),
                        response,
                        idle_ttl=credential.idle_ttl,
                        credential_kind=credential.kind,
                    )
                with self.assertRaisesRegex(HTTPException, "Admin Key 无效"):
                    await admin_service.redeem_admin_credential(key, _request())

        self.assertEqual(credential.kind, "temporary")
        self.assertEqual(credential.idle_ttl, 15 * 60)
        session_pipeline = fake.pipelines[-1]
        expire = next(command for command in session_pipeline.commands if command[0] == "expire")
        self.assertEqual(expire[1][1], 15 * 60)
        self.assertTrue(all("Max-Age=900" in value for value in response.headers.getlist("set-cookie")))

    async def test_temporary_key_duration_is_limited_to_supported_choices(self):
        with self.assertRaisesRegex(ValueError, "15、30、60 或 120"):
            await admin_service.issue_temporary_admin_key("issuer", 20)

    async def test_temporary_session_refreshes_the_selected_sliding_window(self):
        session = "temporary-session"
        key = "stable-admin-key-123456789"
        redis = AsyncMock()
        redis.hgetall.return_value = {
            "key_hash": hashlib.sha256(key.encode()).hexdigest(),
            "idle_ttl": "1800",
            "credential_kind": "temporary",
        }
        redis.expire.return_value = True
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/v1/media/admin/status",
            "headers": [(b"cookie", f"{admin_service.settings.ADMIN_COOKIE_NAME}={session}".encode())],
            "client": ("203.0.113.8", 12345),
        })
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text(key + "\n", encoding="utf-8")
            with (
                patch.object(admin_service, "ADMIN_KEY_FILE", key_file),
                patch.object(admin_service, "redis_client", redis),
            ):
                returned_hash = await admin_service.require_admin(request)

        expected_hash = hashlib.sha256(session.encode()).hexdigest()
        self.assertEqual(returned_hash, expected_hash)
        redis.expire.assert_awaited_once_with(admin_service.SESSION_PREFIX + expected_hash, 1800)
        self.assertEqual(request.scope["admin_session_ttl"], 1800)
        self.assertEqual(request.scope["admin_credential_kind"], "temporary")

    async def test_rotation_returns_published_key_when_redis_reconciliation_fails(self):
        fake = _Redis()
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text("old-admin-key-123456789\n", encoding="utf-8")
            new_key = "new-admin-key-123456789"
            with (
                patch.object(admin_service, "ADMIN_KEY_FILE", key_file),
                patch.object(admin_service, "redis_client", fake),
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
        fake = _Redis()
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text("old-admin-key-123456789\n", encoding="utf-8")
            generated = ["random-admin-key-a-123456789", "random-admin-key-b-123456789"]
            with (
                patch.object(admin_service, "ADMIN_KEY_FILE", key_file),
                patch.object(admin_service, "redis_client", fake),
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

    async def test_rotation_uses_cross_worker_distributed_lock(self):
        fake = _Redis()
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text("old-admin-key-123456789\n", encoding="utf-8")
            with (
                patch.object(admin_service, "ADMIN_KEY_FILE", key_file),
                patch.object(admin_service, "redis_client", fake),
                patch.object(admin_service, "_replace_admin_sessions", new=AsyncMock()),
            ):
                await admin_service.rotate_admin_key("current", None, None)

        self.assertEqual(fake.lock_name, admin_service.ADMIN_KEY_ROTATION_LOCK_KEY)
        self.assertEqual(
            fake.lock_options["timeout"],
            admin_service.ADMIN_KEY_ROTATION_LOCK_SECONDS,
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
    async def test_shared_transaction_audit_failure_propagates(self):
        connection = _FailingAuditConnection()
        with patch.object(admin_service.logger, "exception") as logged:
            with self.assertRaises(SQLAlchemyError):
                await admin_service.audit("actor", "hide", 1, "music/a", "success", "", _request(), conn=connection)
        logged.assert_not_called()

    async def test_post_action_audit_failure_retains_investigation_evidence(self):
        request = _request()
        request.scope.update(request_id="request-123", trace_id="a" * 32)
        with (
            patch.object(admin_service, "engine", _FailingAuditEngine()),
            patch.object(admin_service.logger, "exception") as logged,
        ):
            await admin_service.audit("actor", "delete", 2, "music/a", "success", "operation=123", request)
        evidence = logged.call_args.kwargs["extra"]["context"]["audit_evidence"]
        self.assertEqual(evidence["sid"], "actor")
        self.assertEqual(evidence["ip"], "203.0.113.8")
        self.assertEqual(evidence["summary"], "music/a")
        self.assertEqual(evidence["detail"], "operation=123")
        self.assertEqual(evidence["request_id"], "request-123")
        self.assertEqual(evidence["trace_id"], "a" * 32)

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
