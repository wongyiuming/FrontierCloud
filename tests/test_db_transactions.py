import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core import db


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def __iter__(self):
        return iter(self.rows)


class _BootstrapConnection:
    def __init__(self, *, tables=(), generation=None):
        self.tables = set(tables)
        self.generation = generation
        self.executed = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "FROM information_schema.tables" in sql:
            return _Rows((name,) for name in sorted(self.tables))

        parameters = params or {}
        self.executed.append((sql, parameters))
        created = re.search(
            r"CREATE TABLE(?: IF NOT EXISTS)?\s+`?([A-Za-z0-9_]+)`?",
            sql,
            re.IGNORECASE,
        )
        if created:
            self.tables.add(created.group(1))
        if "INSERT INTO frontiercloud_schema" in sql:
            self.generation = parameters["generation"]
        return _Rows()

    async def scalar(self, statement, _params=None):
        sql = str(statement)
        if "SELECT generation FROM frontiercloud_schema" in sql:
            return self.generation
        raise AssertionError(f"Unexpected scalar query: {sql}")

    async def commit(self):
        self.commits += 1


class _ConnectContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _BootstrapEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return _ConnectContext(self.connection)


class SchemaBootstrapTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_database_bootstraps_complete_current_schema(self):
        connection = _BootstrapConnection()
        with patch.object(db, "engine", _BootstrapEngine(connection)):
            await db.init_db()

        schema = "\n".join(sql for sql, _params in connection.executed)
        self.assertEqual(connection.generation, db.SCHEMA_GENERATION)
        self.assertEqual(db._required_tables() - connection.tables, set())
        self.assertIn("CREATE TABLE frontiercloud_schema", schema)
        self.assertIn("storage_member_id", schema)
        self.assertIn("CHECK (preference BETWEEN -7 AND 500)", schema)
        self.assertIn("UNIQUE INDEX uq_ip_ban_active_ip", schema)
        self.assertIn("idx_webrtc_client_time", schema)
        self.assertNotIn("ALTER TABLE", schema)

    async def test_existing_unmarked_database_is_rejected_without_mutation(self):
        connection = _BootstrapConnection(tables={"karaoke_recordings"})
        with patch.object(db, "engine", _BootstrapEngine(connection)):
            with self.assertRaisesRegex(RuntimeError, "不支持数据库迁移"):
                await db.init_db()
        self.assertEqual(connection.executed, [])

    async def test_current_initialized_database_is_read_only_on_startup(self):
        connection = _BootstrapConnection(
            tables=db._required_tables(),
            generation=db.SCHEMA_GENERATION,
        )
        with patch.object(db, "engine", _BootstrapEngine(connection)):
            await db.init_db()
        self.assertEqual(connection.executed, [])
        self.assertEqual(connection.commits, 0)

    async def test_generation_mismatch_is_rejected_without_mutation(self):
        connection = _BootstrapConnection(
            tables=db._required_tables(),
            generation=db.SCHEMA_GENERATION + 1,
        )
        with patch.object(db, "engine", _BootstrapEngine(connection)):
            with self.assertRaisesRegex(RuntimeError, "初始化代际不兼容"):
                await db.init_db()
        self.assertEqual(connection.executed, [])

    async def test_missing_current_table_is_rejected_without_mutation(self):
        tables = db._required_tables() - {"karaoke_recordings"}
        connection = _BootstrapConnection(
            tables=tables,
            generation=db.SCHEMA_GENERATION,
        )
        with patch.object(db, "engine", _BootstrapEngine(connection)):
            with self.assertRaisesRegex(RuntimeError, "karaoke_recordings"):
                await db.init_db()
        self.assertEqual(connection.executed, [])

    async def test_close_db_disposes_the_connection_pool(self):
        fake_engine = MagicMock()
        fake_engine.dispose = AsyncMock()
        with patch.object(db, "engine", fake_engine):
            await db.close_db()
        fake_engine.dispose.assert_awaited_once()


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
