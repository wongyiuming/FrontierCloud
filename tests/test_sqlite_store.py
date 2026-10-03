import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import db, schema_migrations
from app.core.config import Settings
from app.services import media_objects
from app.services import ip_security
from redis.exceptions import RedisError
from app.store.database import create_database_engine, write_transaction
from app.store.schema import schema_statements, schema_tables
from app.store.sqlite_schema import initialize_schema


class SQLiteStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.configuration = Settings(_env_file=None, DB_TYPE="sqlite", SQLITE_PATH=str(Path(self.directory.name) / "db.sqlite"))
        self.database = create_database_engine(self.configuration)
        self.addAsyncCleanup(self.database.dispose)
        await initialize_schema(self.database, db.SCHEMA_GENERATION)

    async def test_shared_schema_contains_every_required_table_and_constraints(self):
        self.assertEqual(schema_tables("mysql", 2), schema_tables("sqlite", 2))
        self.assertEqual(schema_tables("sqlite", 2), db._required_tables())
        async with self.database.connect() as connection:
            for statement in schema_statements("mysql", 2):
                table = re.match(r"CREATE TABLE(?: IF NOT EXISTS)?\s+`?(\w+)", statement).group(1)
                expected = set(re.findall(
                    r"(?m)^\s+`?(\w+)`?\s+(?:VARCHAR|CHAR|TEXT|JSON|DATETIME|BIGINT|INT|INTEGER|TINYINT|SMALLINT|BLOB|LONGBLOB)\b", statement))
                actual = {row[1] for row in await connection.execute(text(f"PRAGMA table_xinfo({table})"))}
                self.assertEqual(actual, expected, table)
            for pragma, expected in {"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": 5000, "synchronous": 1}.items():
                self.assertEqual(await connection.scalar(text("PRAGMA " + pragma)), expected)
            self.assertEqual(await connection.scalar(text("SELECT GREATEST(0, -3, 2)")), 2)
            self.assertIsNone(await connection.scalar(text("SELECT GREATEST(0, NULL, 2)")))
            columns = await connection.execute(text("PRAGMA table_xinfo(ip_auto_ban_events)"))
            self.assertIn("active_ip_address", {row[1] for row in columns})
        async with write_transaction(self.database) as connection:
            with self.assertRaises(IntegrityError):
                await connection.execute(text("""INSERT INTO media_playback_stats
                    (media_id, media_path, preference, created_at, updated_at)
                    VALUES ('a', 'music/a.mp3', 501, '2026-10-01', '2026-10-01')"""))

    async def test_registry_is_case_sensitive_and_preserves_legacy_ids(self):
        legacy_path = "music/歌手/旧歌.mp3"
        legacy_id = media_objects.legacy_object_id(legacy_path)
        async with write_transaction(self.database) as connection:
            await connection.execute(text("""INSERT INTO media_playback_stats
                (media_id, media_path, created_at, updated_at) VALUES (:id, :path, :now, :now)"""),
                {"id": legacy_id, "path": legacy_path, "now": "2026-10-01 00:00:00"})
        objects = await media_objects.ensure_objects([
            (legacy_path, "audio"), ("music/歌手/A.mp3", "audio"), ("music/歌手/a.mp3", "audio"),
        ], database=self.database)
        self.assertEqual(objects[legacy_path], legacy_id)
        self.assertNotEqual(objects["music/歌手/A.mp3"], objects["music/歌手/a.mp3"])
        self.assertEqual(objects, await media_objects.ensure_objects([(p, "audio") for p in objects], database=self.database))

    async def test_independent_workers_register_one_persistent_identity(self):
        worker = create_database_engine(self.configuration)
        try:
            await asyncio.gather(initialize_schema(self.database, 2), initialize_schema(worker, 2))
            results = await asyncio.gather(*[
                media_objects.ensure_objects([("music/歌手/同一首歌.mp3", "audio")], database=pool)
                for pool in [self.database, worker] * 6
            ])
            self.assertTrue(all(result == results[0] for result in results))
            async with worker.connect() as connection:
                self.assertEqual(await connection.scalar(text("SELECT COUNT(*) FROM media_objects")), 1)
        finally:
            await worker.dispose()

    async def test_write_failure_rolls_back_the_registry(self):
        with self.assertRaisesRegex(RuntimeError, "abort"):
            async with write_transaction(self.database) as connection:
                await media_objects.ensure_object(connection, "music/歌手/回滚.mp3", "audio")
                raise RuntimeError("abort")
        async with self.database.connect() as connection:
            self.assertEqual(await connection.scalar(text("SELECT COUNT(*) FROM media_objects")), 0)

    async def test_generation_one_upgrade_and_future_version_rejection(self):
        async with write_transaction(self.database) as connection:
            await connection.execute(text("DROP TABLE frontiercloud_schema_migrations"))
            await connection.execute(text("UPDATE frontiercloud_schema SET generation=1 WHERE singleton=1"))
        await initialize_schema(self.database, 2)
        async with self.database.connect() as connection:
            self.assertEqual(await connection.scalar(text("SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=2")), schema_migrations.MIGRATIONS[2].checksum)
        async with write_transaction(self.database) as connection:
            await connection.execute(text("UPDATE frontiercloud_schema SET generation=3 WHERE singleton=1"))
        with self.assertRaisesRegex(RuntimeError, "禁止旧版本"):
            await initialize_schema(self.database, 2)
        async with self.database.connect() as connection:
            self.assertEqual(await connection.scalar(text("SELECT generation FROM frontiercloud_schema")), 3)

    async def test_missing_table_is_rejected(self):
        async with write_transaction(self.database) as connection:
            await connection.execute(text("DROP TABLE karaoke_users"))
        with self.assertRaisesRegex(RuntimeError, "karaoke_users"):
            await initialize_schema(self.database, 2)

    async def test_unmarked_nonempty_database_is_rejected(self):
        async with write_transaction(self.database) as connection:
            await connection.execute(text("DROP TABLE frontiercloud_schema"))
        with self.assertRaisesRegex(RuntimeError, "无代际标记"):
            await initialize_schema(self.database, 2)

    async def test_python_entrypoint_selects_the_sqlite_schema(self):
        with patch.object(db, "engine", self.database), patch.object(db, "settings", self.configuration):
            await db.init_db()

    async def test_real_sqlite_master_promotion_without_test_sql_translation(self):
        from cryptography.fernet import Fernet
        from app.services.federation.state import State
        from app.api.v1 import media as media_api
        state = State(self.database, Fernet.generate_key())
        await state.initialize()
        media_root = Path(self.directory.name) / "media"
        media_root.mkdir()
        category = media_root / "music" / "fixture"
        category.mkdir(parents=True)
        (category / "track.mp3").write_bytes(b"disposable media fixture")
        with patch.object(media_api, "MEDIA_ROOT", media_root):
            await state.promote("Master", "https://master.fleet.invalid", "session", 1024**3)
        self.assertEqual((await state.read_existing_identity())["role"], "Master")
        async with self.database.connect() as connection:
            self.assertEqual(await connection.scalar(text("SELECT COUNT(*) FROM global_media_objects")), 1)

    async def test_independent_sqlite_control_writers_lock_before_reading(self):
        from cryptography.fernet import Fernet
        from app.services.federation.state import State
        key = Fernet.generate_key()
        first = State(self.database, key)
        await first.initialize()
        await first.promote("Follower", "https://follower.fleet.invalid", "session")
        worker = create_database_engine(self.configuration)
        self.addAsyncCleanup(worker.dispose)
        second = State(worker, key)
        await second.initialize()
        packages = await asyncio.gather(*[
            state.create_pair("session") for state in [first, second] * 8
        ])
        self.assertEqual(len({item["payload"]["nonce"] for item in packages}), 16)
        async with worker.connect() as connection:
            self.assertEqual(await connection.scalar(text("SELECT COUNT(*) FROM node_pair_packages")), 16)

    async def test_real_sqlite_registration_ledger_survives_concurrent_failures(self):
        from app.services import karaoke_accounts
        from types import SimpleNamespace
        with patch.object(karaoke_accounts, "state", SimpleNamespace(database=self.database)):
            await asyncio.gather(*[karaoke_accounts._record_registration_failure("203.0.113.93") for _ in range(16)])
            self.assertEqual(await karaoke_accounts._registration_counts("203.0.113.93"), (16, 0))

    async def test_security_startup_and_durable_transitions_on_sqlite(self):
        redis = MagicMock()
        redis.lock.return_value.acquire = AsyncMock(side_effect=RedisError("offline"))
        redis.set = AsyncMock()
        redis.delete = AsyncMock()
        async def empty_scan(**_kwargs):
            for value in ():
                yield value
        redis.scan_iter = empty_scan
        pipe = MagicMock()
        pipe.execute = AsyncMock(return_value=[])
        redis.pipeline.return_value = pipe
        with (patch.object(ip_security, "engine", self.database),
              patch.object(ip_security, "redis_client", redis),
              patch.object(ip_security, "publish_edge_snapshot", AsyncMock()),
              patch.object(ip_security, "append_admin_log")):
            await ip_security.initialize_ip_security_cache()
            await ip_security.manual_ban_ip("203.0.113.91", "session", "test")
            block = await ip_security._mysql_block_fallback("203.0.113.91")
            self.assertEqual(block["ban_kind"], "manual")
            await ip_security._hydrate_ip_security_cache()
            await ip_security.add_whitelist("203.0.113.91", "session", "first")
            await ip_security.add_whitelist("203.0.113.91", "session", "updated")
            self.assertIsNone(await ip_security._mysql_block_fallback("203.0.113.91"))
            with self.assertRaises(ValueError):
                await ip_security.manual_permanent_ban_ip("203.0.113.91", "session", "test")
            await ip_security.remove_whitelist("203.0.113.91", "session")
            await ip_security.manual_permanent_ban_ip("203.0.113.91", "session", "test")
            self.assertTrue((await ip_security._mysql_block_fallback("203.0.113.91"))["permanent"])
            await ip_security.unban_ip("203.0.113.91", "session")
            self.assertIsNone(await ip_security._mysql_block_fallback("203.0.113.91"))
            await ip_security.record_invalid_api("203.0.113.92", "GET", "/invalid", "test")
            await ip_security.record_invalid_api("203.0.113.92", "GET", "/invalid", "test")
            await ip_security.add_whitelist("10.199.0.1", "session")
            await ip_security.add_whitelist("13.11.0.1", "session")
            await ip_security.add_whitelist("2001:db8::1", "session")
            summary = await ip_security.list_security_summary(status_filter="whitelisted")
            self.assertEqual([row["ip"] for row in summary["whitelist"]], ["10.199.0.1", "13.11.0.1", "2001:db8::1"])
        async with self.database.connect() as connection:
            self.assertEqual(await connection.scalar(text("SELECT attack_count FROM ip_security_summary")), 2)
            self.assertEqual(await connection.scalar(text("SELECT COUNT(*) FROM ip_permanent_whitelist")), 3)
            self.assertEqual(await connection.scalar(text("SELECT COUNT(*) FROM ip_security_audit_log WHERE action='whitelist_add'")), 5)


class DatabaseConfigurationTests(unittest.TestCase):
    def test_database_selection_rejects_invalid_backends_and_memory_files(self):
        for values in [{"DB_TYPE": "postgres"}, {"DB_TYPE": "sqlite", "SQLITE_PATH": ":memory:"}]:
            with self.assertRaises(ValueError):
                Settings(_env_file=None, **values)

    def test_mysql_endpoint_is_configurable(self):
        configuration = Settings(_env_file=None, MYSQL_HOST="database.example", MYSQL_PORT=3307)
        self.assertIn("@database.example:3307/", configuration.MYSQL_URL)


if __name__ == "__main__":
    unittest.main()
