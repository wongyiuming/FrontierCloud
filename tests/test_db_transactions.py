import os
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db, schema_migrations


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def __iter__(self):
        return iter(self.rows)


class _BootstrapConnection:
    def __init__(self, *, tables=(), generation=None, lock_acquired=1):
        self.tables = set(tables)
        self.generation = generation
        self.lock_acquired = lock_acquired
        self.executed = []
        self.scalar_queries = []
        self.commits = 0
        self.rollbacks = 0

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
        if "INSERT INTO frontiercloud_schema(" in sql:
            self.generation = parameters["generation"]
        if "UPDATE `frontiercloud_schema`" in sql:
            if self.generation == parameters["current"]:
                self.generation = parameters["target"]
        return _Rows()

    async def scalar(self, statement, _params=None):
        sql = str(statement)
        self.scalar_queries.append(sql)
        if "SELECT generation FROM frontiercloud_schema" in sql:
            return self.generation
        if "SELECT generation FROM `frontiercloud_schema`" in sql:
            return self.generation
        if "GET_LOCK(" in sql:
            return self.lock_acquired
        if "RELEASE_LOCK(" in sql:
            return 1
        raise AssertionError(f"Unexpected scalar query: {sql}")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


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
        self.assertIn("CREATE TABLE IF NOT EXISTS frontiercloud_schema_migrations", schema)
        self.assertIn("storage_member_id", schema)
        self.assertIn("CHECK (preference BETWEEN -7 AND 500)", schema)
        self.assertIn("UNIQUE INDEX uq_ip_ban_active_ip", schema)
        self.assertIn("idx_webrtc_client_time", schema)
        self.assertNotIn("ALTER TABLE", schema)

    async def test_existing_unmarked_database_is_rejected_without_mutation(self):
        connection = _BootstrapConnection(tables={"karaoke_recordings"})
        with patch.object(db, "engine", _BootstrapEngine(connection)):
            with self.assertRaisesRegex(RuntimeError, "无代际标记数据库自动迁移"):
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
        self.assertFalse(any("GET_LOCK(" in sql for sql in connection.scalar_queries))

    async def test_generation_one_database_migrates_in_place_to_current(self):
        generation_one_tables = db._required_tables() - {schema_migrations.MIGRATION_HISTORY_TABLE}
        connection = _BootstrapConnection(tables=generation_one_tables, generation=1)
        with patch.object(db, "engine", _BootstrapEngine(connection)):
            await db.init_db()

        schema = "\n".join(sql for sql, _params in connection.executed)
        self.assertEqual(connection.generation, db.SCHEMA_GENERATION)
        self.assertIn(schema_migrations.MIGRATION_HISTORY_TABLE, connection.tables)
        self.assertIn("CREATE TABLE IF NOT EXISTS frontiercloud_schema_migrations", schema)
        self.assertIn("UPDATE `frontiercloud_schema`", schema)
        self.assertIn("INSERT INTO `frontiercloud_schema_migrations`", schema)
        self.assertTrue(any("GET_LOCK(" in sql for sql in connection.scalar_queries))
        self.assertTrue(any("RELEASE_LOCK(" in sql for sql in connection.scalar_queries))
        self.assertEqual(db._required_tables() - connection.tables, set())

    async def test_future_generation_is_rejected_without_mutation(self):
        connection = _BootstrapConnection(
            tables=db._required_tables(),
            generation=db.SCHEMA_GENERATION + 1,
        )
        with patch.object(db, "engine", _BootstrapEngine(connection)):
            with self.assertRaisesRegex(RuntimeError, "禁止旧版本应用启动或降级"):
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

    async def test_migration_lock_timeout_keeps_old_generation(self):
        generation_one_tables = db._required_tables() - {schema_migrations.MIGRATION_HISTORY_TABLE}
        connection = _BootstrapConnection(
            tables=generation_one_tables,
            generation=1,
            lock_acquired=0,
        )
        with patch.object(db, "engine", _BootstrapEngine(connection)):
            with self.assertRaisesRegex(RuntimeError, "等待数据库迁移锁超时"):
                await db.init_db()
        self.assertEqual(connection.generation, 1)
        self.assertNotIn(schema_migrations.MIGRATION_HISTORY_TABLE, connection.tables)

    async def test_failed_migration_does_not_advance_generation(self):
        async def fail(_conn):
            raise RuntimeError("simulated migration failure")

        migration = schema_migrations.SchemaMigration(
            target_generation=2,
            name="simulated-failure",
            signature="simulated-failure-v1",
            apply=fail,
        )
        generation_one_tables = db._required_tables() - {schema_migrations.MIGRATION_HISTORY_TABLE}
        connection = _BootstrapConnection(tables=generation_one_tables, generation=1)
        with (
            patch.object(schema_migrations, "MIGRATIONS", {2: migration}),
            patch.object(db, "engine", _BootstrapEngine(connection)),
        ):
            with self.assertRaisesRegex(RuntimeError, "generation marker 未推进"):
                await db.init_db()

        self.assertEqual(connection.generation, 1)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(any("RELEASE_LOCK(" in sql for sql in connection.scalar_queries))

    async def test_close_db_disposes_the_connection_pool(self):
        fake_engine = MagicMock()
        fake_engine.dispose = AsyncMock()
        with patch.object(db, "engine", fake_engine):
            await db.close_db()
        fake_engine.dispose.assert_awaited_once()


@unittest.skipUnless(
    os.getenv("INSTANCE_NAME") == "frontiercloud-ci",
    "real MySQL migration smoke runs only in the disposable CI stack",
)
class MysqlSchemaMigrationSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_one_upgrades_against_real_mysql(self):
        # The application process has already used db.engine on its own event loop.
        # IsolatedAsyncioTestCase owns a different loop, so this smoke test must use
        # a fresh pool created and disposed entirely inside this test loop.
        test_engine = create_async_engine(db.settings.MYSQL_URL, pool_pre_ping=True)
        try:
            with patch.object(db, "engine", test_engine):
                async with test_engine.connect() as conn:
                    generation = await conn.scalar(text(
                        "SELECT generation FROM frontiercloud_schema WHERE singleton=1"
                    ))
                    self.assertEqual(int(generation), db.SCHEMA_GENERATION)
                    await conn.execute(text(
                        "DROP TABLE IF EXISTS frontiercloud_schema_migrations"
                    ))
                    await conn.execute(text(
                        "UPDATE frontiercloud_schema SET generation=1 WHERE singleton=1"
                    ))
                    await conn.commit()

                await db.init_db()

                async with test_engine.connect() as conn:
                    generation = await conn.scalar(text(
                        "SELECT generation FROM frontiercloud_schema WHERE singleton=1"
                    ))
                    self.assertEqual(int(generation), db.SCHEMA_GENERATION)
                    history_exists = await conn.scalar(text("""
                        SELECT COUNT(*)
                        FROM information_schema.tables
                        WHERE table_schema=DATABASE()
                          AND table_name='frontiercloud_schema_migrations'
                    """))
                    self.assertEqual(int(history_exists), 1)
                    row = (await conn.execute(text("""
                        SELECT generation, migration_name, checksum
                        FROM frontiercloud_schema_migrations
                        WHERE generation=2
                    """))).one()
                    self.assertEqual(int(row[0]), 2)
                    self.assertEqual(row[1], "create-schema-migration-journal")
                    self.assertEqual(len(row[2]), 64)

                # A second startup at the current generation must be a clean no-op.
                await db.init_db()
        finally:
            await test_engine.dispose()


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
