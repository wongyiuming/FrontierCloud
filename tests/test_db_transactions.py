import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core import db


class _MigrationConnection:
    def __init__(
        self,
        preference_check="(`preference` between -(2) and 7)",
        *,
        get_lock_error=None,
        release_result=1,
        first_commit_error=None,
    ):
        self.preference_check = preference_check
        self.get_lock_error = get_lock_error
        self.release_result = release_result
        self.first_commit_error = first_commit_error
        self.executed = []
        self.scalar_queries = []
        self.commits = 0
        self.rollbacks = 0
        self.invalidations = 0

    async def scalar(self, statement, _params=None):
        sql = str(statement)
        self.scalar_queries.append(sql)
        if "GET_LOCK" in sql:
            if self.get_lock_error is not None:
                raise self.get_lock_error
            return 1
        if "RELEASE_LOCK" in sql:
            return self.release_result
        if "COLLATION_NAME" in sql:
            return "utf8mb4_bin"
        if "information_schema.columns" in sql:
            return 1
        if "information_schema.statistics" in sql:
            return 1
        if "CHECK_CLAUSE" in sql:
            return self.preference_check
        raise AssertionError(f"Unexpected scalar query: {sql}")

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))
        return object()

    async def commit(self):
        self.commits += 1
        if self.first_commit_error is not None and self.commits == 1:
            raise self.first_commit_error

    async def rollback(self):
        self.rollbacks += 1

    async def invalidate(self):
        self.invalidations += 1


class _ConnectContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _MigrationEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return _ConnectContext(self.connection)


class SchemaMigrationTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancellation_does_not_cancel_lock_cleanup(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def cleanup():
            started.set()
            await finish.wait()
            return True

        cleanup_task = asyncio.create_task(cleanup())
        waiter = asyncio.create_task(db._await_cleanup_task(cleanup_task))
        await started.wait()
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        self.assertFalse(cleanup_task.cancelled())
        finish.set()
        completed, cancellation = await waiter
        self.assertTrue(completed)
        self.assertIsInstance(cancellation, asyncio.CancelledError)

    async def test_constraint_replacement_is_one_atomic_mysql_ddl(self):
        connection = _MigrationConnection("(`preference` between 0 and 7)")
        with patch.object(db, "engine", _MigrationEngine(connection)):
            await db.init_db()

        alterations = [
            sql
            for sql, _params in connection.executed
            if "ALTER TABLE media_playback_stats" in sql
        ]
        self.assertEqual(len(alterations), 1)
        self.assertIn("DROP CHECK chk_media_preference", alterations[0])
        self.assertIn("ADD CONSTRAINT chk_media_preference", alterations[0])
        self.assertTrue(any("RELEASE_LOCK" in sql for sql in connection.scalar_queries))

    async def test_exact_constraint_does_not_trigger_destructive_ddl(self):
        connection = _MigrationConnection()
        with patch.object(db, "engine", _MigrationEngine(connection)):
            await db.init_db()

        alterations = [
            sql
            for sql, _params in connection.executed
            if "ALTER TABLE media_playback_stats" in sql
        ]
        self.assertEqual(alterations, [])

    async def test_schema_declares_one_active_ban_per_ip(self):
        connection = _MigrationConnection()
        with patch.object(db, "engine", _MigrationEngine(connection)):
            await db.init_db()

        schema = "\n".join(sql for sql, _params in connection.executed)
        self.assertIn("CREATE TABLE IF NOT EXISTS ip_security_locks", schema)
        self.assertIn("GENERATED ALWAYS AS", schema)
        self.assertIn("UNIQUE INDEX uq_ip_ban_active_ip", schema)

    async def test_close_db_disposes_the_connection_pool(self):
        fake_engine = MagicMock()
        fake_engine.dispose = AsyncMock()
        with patch.object(db, "engine", fake_engine):
            await db.close_db()
        fake_engine.dispose.assert_awaited_once()

    async def test_commit_failure_after_get_lock_still_releases_session_lock(self):
        connection = _MigrationConnection(first_commit_error=RuntimeError("commit failed"))
        with patch.object(db, "engine", _MigrationEngine(connection)):
            with self.assertRaises(RuntimeError):
                await db.init_db()

        self.assertTrue(any("RELEASE_LOCK" in sql for sql in connection.scalar_queries))
        self.assertEqual(connection.invalidations, 0)

    async def test_unknown_get_lock_outcome_invalidates_physical_connection(self):
        connection = _MigrationConnection(get_lock_error=RuntimeError("connection lost"))
        with patch.object(db, "engine", _MigrationEngine(connection)):
            with self.assertRaises(RuntimeError):
                await db.init_db()

        self.assertFalse(any("RELEASE_LOCK" in sql for sql in connection.scalar_queries))
        self.assertEqual(connection.invalidations, 1)

    async def test_unconfirmed_release_invalidates_physical_connection(self):
        connection = _MigrationConnection(release_result=0)
        with patch.object(db, "engine", _MigrationEngine(connection)):
            with self.assertRaises(RuntimeError):
                await db.init_db()

        self.assertEqual(connection.invalidations, 1)


class LifespanCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_still_disposes_database_pool(self):
        import main

        with (
            patch.object(main, "init_db", new=AsyncMock()),
            patch.object(
                main,
                "recover_interrupted_media_deletions",
                new=AsyncMock(side_effect=RuntimeError("recovery failed")),
            ),
            patch.object(main, "close_db", new=AsyncMock()) as close_db,
        ):
            with self.assertRaises(RuntimeError):
                async with main.lifespan(main.app):
                    pass
        close_db.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
