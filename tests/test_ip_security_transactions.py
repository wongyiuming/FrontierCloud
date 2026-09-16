from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.services import ip_security


@asynccontextmanager
async def _unlocked_guard(*_args, **_kwargs):
    yield


class _TransactionContext:
    def __init__(self, connection):
        self.connection = connection
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, _exc, _traceback):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False


class _Engine:
    def __init__(self, connection):
        self.transaction = _TransactionContext(connection)

    def begin(self):
        return self.transaction

    def connect(self):
        return self.transaction


class _Connection:
    def __init__(self):
        self.executed = []

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))
        return type("Result", (), {"lastrowid": 7})()

    async def scalar(self, *_args, **_kwargs):
        return 0


class SecurityStateTransactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        publisher = patch.object(ip_security, "publish_edge_snapshot", new=AsyncMock())
        self.publish_edge = publisher.start()
        self.addCleanup(publisher.stop)

    async def test_normal_mutation_projects_only_changed_ip_and_persists_retry_intent(self):
        connection = _Connection()
        fake_engine = _Engine(connection)
        project = AsyncMock(side_effect=RedisError("cache unavailable"))
        hydrate = AsyncMock()
        with (
            patch.object(ip_security, "engine", fake_engine),
            patch.object(ip_security, "redis_client", new=AsyncMock()),
            patch.object(ip_security, "_security_state_guard", _unlocked_guard),
            patch.object(ip_security, "_project_ip", project),
            patch.object(ip_security, "_hydrate_ip_security_cache", hydrate),
            patch.object(ip_security, "append_admin_log"),
        ):
            result = await ip_security._run_state_transaction(AsyncMock(return_value="committed"), ip="203.0.113.90")
        self.assertEqual(result, "committed")
        self.assertTrue(fake_engine.transaction.committed)
        project.assert_awaited_once_with("203.0.113.90")
        hydrate.assert_not_awaited()
        self.publish_edge.assert_not_awaited()
        sql = "\n".join(statement for statement, _params in connection.executed)
        self.assertIn("projection_dirty=1", sql)
        self.assertIn("generation=generation+1", sql)

    async def test_redis_outage_does_not_prevent_mysql_invalid_event_and_ban(self):
        class Connection(_Connection):
            values = iter([0, 5, 0, 0, 0, 0])
            async def scalar(self, *_args, **_kwargs):
                return next(self.values, 0)
        connection = Connection()
        engine = _Engine(connection)
        redis = AsyncMock()
        redis.set.side_effect = RedisError("offline")
        with (
            patch.object(ip_security, "engine", engine),
            patch.object(ip_security, "redis_client", redis),
            patch.object(ip_security, "_security_state_guard", _unlocked_guard),
            patch.object(ip_security, "_project_ip", AsyncMock(side_effect=RedisError("offline"))),
            patch.object(ip_security, "append_admin_log"),
        ):
            count = await ip_security.record_invalid_api("203.0.113.90", "GET", "/unknown", "scanner")
        self.assertEqual(count, 6)
        self.assertTrue(engine.transaction.committed)
        events = [params for statement, params in connection.executed if "INSERT INTO ip_security_audit_log" in statement]
        self.assertEqual([event["action"] for event in events], ["invalid_api", "automatic_ban"])
        self.assertTrue(any("INSERT INTO ip_security_summary" in sql for sql, _params in connection.executed))
        self.assertTrue(any("INSERT INTO ip_auto_ban_events" in sql for sql, _params in connection.executed))

    async def test_independent_ips_enter_state_guard_concurrently(self):
        class Lock:
            def __init__(self, name):
                self.name = name
                self.local = type("Local", (), {"token": b"owned"})()
            async def acquire(self):
                return True
            async def release(self):
                return True
        redis = MagicMock()
        redis.lock.side_effect = lambda name, **_kwargs: Lock(name)
        entered = [asyncio.Event(), asyncio.Event()]
        async def work(index):
            async with ip_security._security_state_guard(f"203.0.113.{index + 1}"):
                entered[index].set()
                await asyncio.wait_for(entered[1 - index].wait(), 1)
        with patch.object(ip_security, "redis_client", redis):
            await asyncio.gather(work(0), work(1))
        names = [call.args[0] for call in redis.lock.call_args_list]
        self.assertEqual(len(set(names)), 2)

    async def test_projection_clears_durable_retry_only_after_redis_success(self):
        connection = _Connection()
        engine = _Engine(connection)
        pipe = _PipelineRecorder()
        async def execute():
            connection.executed.append(("REDIS_COMMIT", {}))
            return [True] * len(pipe.commands)
        pipe.execute = execute
        redis = MagicMock()
        redis.pipeline.return_value = pipe
        with (
            patch.object(ip_security, "engine", engine),
            patch.object(ip_security, "redis_client", redis),
            patch.object(ip_security, "_mysql_block_fallback", AsyncMock(return_value=None)),
        ):
            await ip_security._project_ip("203.0.113.90")
        sql = [statement for statement, _params in connection.executed]
        self.assertTrue(engine.transaction.committed)
        self.assertLess(next(i for i, value in enumerate(sql) if "ip_security_locks" in value), sql.index("REDIS_COMMIT"))
        self.assertLess(sql.index("REDIS_COMMIT"), next(i for i, value in enumerate(sql) if "projection_dirty=0" in value))

    async def test_failed_projection_preserves_durable_retry(self):
        connection = _Connection()
        engine = _Engine(connection)
        pipe = _PipelineRecorder()
        pipe.execute = AsyncMock(side_effect=RedisError("offline"))
        redis = MagicMock()
        redis.pipeline.return_value = pipe
        with (
            patch.object(ip_security, "engine", engine),
            patch.object(ip_security, "redis_client", redis),
            patch.object(ip_security, "_mysql_block_fallback", AsyncMock(return_value=None)),
        ):
            with self.assertRaises(RedisError):
                await ip_security._project_ip("203.0.113.90")
        self.assertTrue(engine.transaction.rolled_back)
        self.assertFalse(any("projection_dirty=0" in sql for sql, _params in connection.executed))

    async def test_lost_per_ip_projection_lease_cannot_publish(self):
        connection = _Connection()
        engine = _Engine(connection)
        pipe = _PipelineRecorder()
        pipe.watch = AsyncMock()
        pipe.get = AsyncMock(return_value="new-owner")
        pipe.reset = AsyncMock()
        pipe.execute = AsyncMock()
        redis = MagicMock()
        redis.pipeline.return_value = pipe
        lock = MagicMock()
        lock.name = ip_security.CACHE_LOCK_KEY + ":203.0.113.90"
        lock.local.token = b"old-owner"
        token = ip_security._CACHE_LOCK.set(lock)
        try:
            with (
                patch.object(ip_security, "engine", engine),
                patch.object(ip_security, "redis_client", redis),
                patch.object(ip_security, "_mysql_block_fallback", AsyncMock(return_value=None)),
            ):
                with self.assertRaises(RedisError):
                    await ip_security._project_ip("203.0.113.90")
        finally:
            ip_security._CACHE_LOCK.reset(token)
        pipe.execute.assert_not_awaited()
        pipe.reset.assert_awaited_once()

    async def test_db_commit_survives_cache_refresh_failure_and_cache_stays_dirty(self):
        connection = _Connection()
        fake_engine = _Engine(connection)
        fake_redis = AsyncMock()
        hydrate = AsyncMock(side_effect=RedisError("redis unavailable"))

        async def operation(conn):
            await conn.execute("UPDATE durable_state")
            return "committed"

        with (
            patch.object(ip_security, "engine", fake_engine),
            patch.object(ip_security, "redis_client", fake_redis),
            patch.object(ip_security, "_security_state_guard", _unlocked_guard),
            patch.object(ip_security, "_hydrate_ip_security_cache", hydrate),
            patch.object(ip_security, "append_admin_log"),
        ):
            result = await ip_security._run_state_transaction(operation)

        self.assertEqual(result, "committed")
        self.assertTrue(fake_engine.transaction.committed)
        fake_redis.delete.assert_awaited_once_with(ip_security.CACHE_READY_KEY)
        hydrate.assert_awaited_once()

    async def test_edge_publication_is_deferred_and_retries_remain_durable(self):
        fake_engine = _Engine(_Connection())
        with (
            patch.object(ip_security, "engine", fake_engine),
            patch.object(ip_security, "redis_client", new=AsyncMock()),
            patch.object(ip_security, "_security_state_guard", _unlocked_guard),
            patch.object(ip_security, "_hydrate_ip_security_cache", new=AsyncMock()),
        ):
            result = await ip_security._run_state_transaction(AsyncMock(return_value="committed"))
            self.publish_edge.assert_not_awaited()
            self.assertTrue(ip_security._edge_projection_dirty)
            self.publish_edge.side_effect = OSError("snapshot disk unavailable")
            with self.assertRaises(OSError):
                await ip_security._refresh_edge_projection()
            self.assertTrue(ip_security._edge_projection_dirty)
            self.publish_edge.side_effect = None
            await ip_security._refresh_edge_projection()
        self.assertEqual(result, "committed")
        self.assertTrue(fake_engine.transaction.committed)
        self.assertFalse(ip_security._edge_projection_dirty)

    async def test_db_failure_rolls_back_and_recovers_the_previous_projection(self):
        connection = _Connection()
        fake_engine = _Engine(connection)
        fake_redis = AsyncMock()
        hydrate = AsyncMock()

        async def operation(_conn):
            raise SQLAlchemyError("write failed")

        with (
            patch.object(ip_security, "engine", fake_engine),
            patch.object(ip_security, "redis_client", fake_redis),
            patch.object(ip_security, "_security_state_guard", _unlocked_guard),
            patch.object(ip_security, "_hydrate_ip_security_cache", hydrate),
        ):
            with self.assertRaises(SQLAlchemyError):
                await ip_security._run_state_transaction(operation)

        self.assertTrue(fake_engine.transaction.rolled_back)
        hydrate.assert_awaited_once()

    async def test_db_failure_does_not_clear_violation_counter(self):
        connection = _Connection()
        fake_engine = _Engine(connection)
        fake_redis = AsyncMock()

        async def operation(_conn):
            raise SQLAlchemyError("write failed")

        with (
            patch.object(ip_security, "engine", fake_engine),
            patch.object(ip_security, "redis_client", fake_redis),
            patch.object(ip_security, "_security_state_guard", _unlocked_guard),
            patch.object(ip_security, "_hydrate_ip_security_cache", new=AsyncMock()),
        ):
            with self.assertRaises(SQLAlchemyError):
                await ip_security._run_state_transaction(
                    operation,
                    clear_violations_for="203.0.113.32",
                )

        fake_redis.delete.assert_awaited_once_with(ip_security.CACHE_READY_KEY)

    async def test_violation_counter_is_cleared_only_after_db_commit(self):
        connection = _Connection()
        fake_engine = _Engine(connection)
        fake_redis = AsyncMock()

        async def operation(_conn):
            self.assertEqual(fake_redis.delete.await_count, 1)
            return "committed"

        with (
            patch.object(ip_security, "engine", fake_engine),
            patch.object(ip_security, "redis_client", fake_redis),
            patch.object(ip_security, "_security_state_guard", _unlocked_guard),
            patch.object(ip_security, "_hydrate_ip_security_cache", new=AsyncMock()),
        ):
            result = await ip_security._run_state_transaction(
                operation,
                clear_violations_for="203.0.113.33",
            )

        self.assertEqual(result, "committed")
        self.assertEqual(fake_redis.delete.await_count, 2)
        fake_redis.delete.assert_any_await(ip_security.CACHE_READY_KEY)
        fake_redis.delete.assert_any_await(
            ip_security._violation_key("203.0.113.33")
        )

    async def test_dirty_cache_lookup_uses_mysql_instead_of_failing_open(self):
        pipeline = MagicMock()
        pipeline.exists.return_value = pipeline
        pipeline.sismember.return_value = pipeline
        pipeline.get.return_value = pipeline
        pipeline.execute = AsyncMock(return_value=[0, 0, 0, None])
        fake_redis = MagicMock()
        fake_redis.pipeline.return_value = pipeline
        expected = {"ip": "203.0.113.30", "permanent": True}

        with (
            patch.object(ip_security, "redis_client", fake_redis),
            patch.object(ip_security, "ensure_ip_security_cache", new=AsyncMock()),
            patch.object(ip_security, "_mysql_block_fallback", new=AsyncMock(return_value=expected)) as fallback,
        ):
            result = await ip_security.get_ip_block("203.0.113.30")

        self.assertEqual(result, expected)
        fallback.assert_awaited_once_with("203.0.113.30")

    async def test_expired_cached_ban_is_not_enforced(self):
        pipeline = MagicMock()
        pipeline.exists.return_value = pipeline
        pipeline.sismember.return_value = pipeline
        pipeline.get.return_value = pipeline
        payload = {
            "ip": "203.0.113.34",
            "expires_at": (
                datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            ).isoformat(),
        }
        pipeline.execute = AsyncMock(return_value=[1, 0, 0, json.dumps(payload)])
        fake_redis = MagicMock()
        fake_redis.pipeline.return_value = pipeline

        with (
            patch.object(ip_security, "redis_client", fake_redis),
            patch.object(ip_security, "ensure_ip_security_cache", new=AsyncMock()),
        ):
            result = await ip_security.get_ip_block("203.0.113.34")

        self.assertIsNone(result)

    async def test_whitelist_and_release_are_one_mysql_transaction_callback(self):
        connection = _Connection()

        async def execute_callback(operation, **kwargs):
            self.assertEqual(kwargs["clear_violations_for"], "203.0.113.31")
            return await operation(connection)

        with patch.object(
            ip_security,
            "_run_state_transaction",
            side_effect=execute_callback,
        ) as transaction:
            value = await ip_security.add_whitelist("203.0.113.31", "session", "trusted")

        self.assertEqual(value, "203.0.113.31")
        transaction.assert_awaited_once()
        sql = "\n".join(statement for statement, _params in connection.executed)
        self.assertIn("INSERT INTO ip_permanent_whitelist", sql)
        self.assertIn("SET status='whitelisted'", sql)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _SnapshotConnection:
    def __init__(self, results=None):
        self.results = results or [_Rows([]), _Rows([]), _Rows([])]

    async def execute(self, *_args, **_kwargs):
        return self.results.pop(0)

    async def scalar(self, *_args, **_kwargs):
        return 0


class _ConnectContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _SnapshotEngine:
    def __init__(self, results=None):
        self.results = results

    def connect(self):
        return _ConnectContext(_SnapshotConnection(self.results))

    def begin(self):
        return self.connect()


class _PipelineRecorder:
    def __init__(self):
        self.commands = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.commands.append((name, args, kwargs))
            return self
        return record

    async def execute(self):
        return [True] * len(self.commands)


class _HydrationRedis:
    def __init__(self):
        self.pipe = _PipelineRecorder()

    async def scan_iter(self, match=None):
        self.match = match
        yield ip_security.BAN_PREFIX + "203.0.113.99"

    def pipeline(self, transaction):
        self.transaction = transaction
        return self.pipe


class SecurityHydrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_lock_cannot_publish_an_old_cache_snapshot(self):
        fake_redis = _HydrationRedis()
        fake_redis.pipe.watch = AsyncMock()
        fake_redis.pipe.get = AsyncMock(return_value="new-owner")
        fake_redis.pipe.reset = AsyncMock()
        fake_redis.pipe.execute = AsyncMock()
        lock = MagicMock()
        lock.local.token = b"old-owner"
        token = ip_security._CACHE_LOCK.set(lock)
        try:
            with (
                patch.object(ip_security, "engine", _SnapshotEngine()),
                patch.object(ip_security, "redis_client", fake_redis),
            ):
                with self.assertRaises(RedisError):
                    await ip_security._hydrate_ip_security_cache()
        finally:
            ip_security._CACHE_LOCK.reset(token)
        fake_redis.pipe.execute.assert_not_awaited()
        fake_redis.pipe.reset.assert_awaited_once()

    async def test_hydration_removes_stale_bans_and_marks_ready_last(self):
        fake_redis = _HydrationRedis()
        with (
            patch.object(ip_security, "engine", _SnapshotEngine()),
            patch.object(ip_security, "redis_client", fake_redis),
        ):
            await ip_security._hydrate_ip_security_cache()

        self.assertTrue(fake_redis.transaction)
        self.assertIn(
            ("delete", (ip_security.BAN_PREFIX + "203.0.113.99",), {}),
            fake_redis.pipe.commands,
        )
        self.assertEqual(
            fake_redis.pipe.commands[-1],
            ("set", (ip_security.CACHE_READY_KEY, "1"), {}),
        )

    async def test_temporary_ban_uses_absolute_redis_expiry(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires_at = now + timedelta(seconds=30)
        ban = {
            "ip_address": "203.0.113.35",
            "trigger_count": 6,
            "window_started_at": now,
            "banned_at": now,
            "expires_at": expires_at,
            "last_method": "GET",
            "last_path": "/invalid",
            "status": "active",
            "ban_kind": "auto",
        }
        fake_redis = _HydrationRedis()
        snapshot = _SnapshotEngine([_Rows([]), _Rows([ban]), _Rows([])])
        with (
            patch.object(ip_security, "engine", snapshot),
            patch.object(ip_security, "redis_client", fake_redis),
        ):
            await ip_security._hydrate_ip_security_cache()

        ban_key = ip_security._ban_key("203.0.113.35")
        self.assertTrue(any(
            name == "set" and args[0] == ban_key and "ex" not in kwargs
            for name, args, kwargs in fake_redis.pipe.commands
        ))
        self.assertTrue(any(
            name == "pexpireat" and args[0] == ban_key
            for name, args, _kwargs in fake_redis.pipe.commands
        ))


if __name__ == "__main__":
    unittest.main()
