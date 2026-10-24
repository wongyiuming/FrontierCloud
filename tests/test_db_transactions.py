import os
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex
from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.core import db, schema_migrations


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def __iter__(self):
        return iter(self.rows)


class _BootstrapConnection:
    dialect = mysql.dialect()

    def __init__(self, *, tables=(), generation=None, lock_acquired=1):
        self.tables = set(tables)
        self.generation = generation
        self.lock_acquired = lock_acquired
        self.executed = []
        self.scalar_queries = []
        self.commits = 0
        self.rollbacks = 0
        self.preference_check = True
        from app.services.resource_pause_schema import CHECK, OLD_CHECK
        self.pause_check = CHECK if generation == db.SCHEMA_GENERATION else OLD_CHECK
        from app.services import resource_migration_schema,s3_schema,node_retirement_schema,resource_cancel_census_schema
        tables = [*resource_migration_schema.metadata.tables.values(),*s3_schema.metadata.tables.values(),
            *node_retirement_schema.metadata.tables.values(),*resource_cancel_census_schema.metadata.tables.values()]
        self.indexes = {index.name for table in tables
                        if table.name in self.tables for index in table.indexes}

    async def run_sync(self, function, **kwargs):
        # This transaction-order fake already assumes complete valid columns
        # for existing tables, as its information_schema.columns response does.
        # Real shape/corruption checks live in SQLite/MySQL runtime fixtures.
        index = getattr(function, '__self__', None)
        if isinstance(index, Index):
            if not kwargs.get('checkfirst') or index.name not in self.indexes:
                await self.execute(CreateIndex(index).compile(dialect=self.dialect))
                self.indexes.add(index.name)
            return
        pause_inspection = (function.__module__ == 'app.services.resource_pause_schema'
                            and function.__qualname__ == 'current.<locals>.<lambda>')
        retirement_inspection=(function.__module__=='app.services.node_retirement_schema' and function.__qualname__=='validate.<locals>.observed')
        census_inspection=(function.__module__=='app.services.resource_cancel_census_schema' and function.__qualname__=='validate.<locals>.verify')
        if function.__name__ != 'inspect_tables' and not pause_inspection and not retirement_inspection and not census_inspection:
            raise AssertionError(f'Unsupported sync operation: {function}')
        from app.services import resource_migration_schema,s3_schema,node_retirement_schema,resource_cancel_census_schema
        tables = {**resource_migration_schema.metadata.tables,**s3_schema.metadata.tables,
            **node_retirement_schema.metadata.tables,**resource_cancel_census_schema.metadata.tables}
        connection = self
        class Inspector:
            def table(self, name):
                if name not in connection.tables:
                    raise AssertionError(f'Missing fixture table: {name}')
                return tables[name]

            def get_columns(self, name):
                return [dict(name=column.name, type=column.type, nullable=column.nullable)
                        for column in self.table(name).columns]

            def get_pk_constraint(self, name):
                return {'constrained_columns': [column.name for column in self.table(name).primary_key]}

            def get_indexes(self, name):
                return [dict(name=index.name,column_names=[column.name for column in index.columns],unique=bool(index.unique)) for index in self.table(name).indexes
                        if index.name in connection.indexes]

            def get_check_constraints(self, name):
                from app.services.resource_pause_schema import NAME
                return [dict(name=constraint.name, sqltext=connection.pause_check
                             if name == 'migration_tasks' and constraint.name == NAME else str(constraint.sqltext))
                        for constraint in self.table(name).constraints
                        if isinstance(constraint, CheckConstraint)]

            def get_unique_constraints(self, name):
                return [dict(column_names=[column.name for column in constraint.columns])
                        for constraint in self.table(name).constraints if isinstance(constraint, UniqueConstraint)]

            def get_foreign_keys(self,name):
                self.table(name);return []

            def get_table_options(self,name):
                self.table(name);return dict(mysql_engine='InnoDB',mysql_collate='utf8mb4_bin')

        class SyncView:
            dialect=connection.dialect
            def scalar(self,statement,parameters):
                # Transaction-order tests assume healthy reflected structures;
                # native MySQL tests separately corrupt these actual properties.
                sql=str(statement)
                if parameters!={'name':'node_retirements'}:raise AssertionError('Unexpected sync inspection parameters')
                if any('FROM information_schema.'+name in sql for name in ('COLUMNS','TRIGGERS','TABLE_CONSTRAINTS')):return 0
                raise AssertionError('Unsupported sync inspection: '+sql)

        with patch.dict('sqlalchemy.inspection._registrars', {SyncView: lambda _subject: Inspector()}):
            return function(SyncView())

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "FROM information_schema.tables" in sql:
            return _Rows((name,) for name in sorted(self.tables))
        if sql.startswith("SELECT generation,migration_name,checksum FROM"):
            return _Rows()

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
        if "DROP CHECK chk_media_preference" in sql:
            self.preference_check = False
        if "ADD CONSTRAINT chk_media_preference" in sql:
            self.preference_check = True
        from app.services.resource_pause_schema import NAME, CHECK
        if sql == f'ALTER TABLE migration_tasks DROP CHECK {NAME}, ADD CONSTRAINT {NAME} CHECK ({CHECK})':
            self.pause_check = CHECK
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
        if "FROM information_schema.tables" in sql:
            return int((_params or {}).get("table_name") in self.tables)
        if "FROM information_schema.columns" in sql:
            return int((_params or {}).get("table_name") in self.tables)
        if "FROM information_schema.TRIGGERS" in sql and "migration_cancel_census" in sql:return None
        if "FROM information_schema.table_constraints" in sql:
            return int(self.preference_check)
        if "FROM media_playback_stats WHERE preference < -7" in sql:
            return 0
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
        self.assertIn("CHECK (preference BETWEEN -7 AND 500000)", schema)
        self.assertIn("UNIQUE INDEX uq_ip_ban_active_ip", schema)
        self.assertIn("idx_webrtc_client_time", schema)
        from app.services.resource_pause_schema import NAME, CHECK
        alters = [sql for sql, _ in connection.executed if sql.startswith('ALTER TABLE')]
        self.assertEqual(alters, [f'ALTER TABLE migration_tasks DROP CHECK {NAME}, ADD CONSTRAINT {NAME} CHECK ({CHECK})'])

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
            patch.object(schema_migrations, "CURRENT_SCHEMA_GENERATION", 2),
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
