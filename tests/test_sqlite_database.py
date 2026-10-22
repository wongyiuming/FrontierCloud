"""Real SQLite migrations, legacy DML and persistence regressions (no public CA)."""
import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import insert, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from app.core import db, schema_migrations
from app.core.config import Settings
from app.core.sqlite_compat import create_sqlite_engine, normalize_statement
from app.core.sqlite_schema import LOCAL_DDL


class SQLiteStatementTests(unittest.TestCase):
    def test_literals_comments_and_bound_values_are_not_rewritten(self):
        literal = "'FOR UPDATE BINARY LEFT(x) VALUES(id) ON DUPLICATE KEY UPDATE'"
        source = "SELECT " + literal + " /* FOR SHARE BINARY */ FROM x WHERE BINARY path=:path FOR UPDATE"
        converted = normalize_statement(source)
        self.assertIn(literal, converted)
        self.assertIn("/* FOR SHARE BINARY */", converted)
        self.assertIn("WHERE path=:path", converted)
        self.assertFalse(converted.endswith("FOR UPDATE"))

    def test_settings_default_and_explicit_mysql(self):
        default = Settings(_env_file=None)
        self.assertEqual(default.DB_TYPE, "sqlite")
        self.assertEqual(default.DATABASE_URL.drivername, "sqlite+aiosqlite")
        legacy = Settings(_env_file=None, DB_TYPE="mysql")
        self.assertEqual(legacy.DATABASE_URL, legacy.MYSQL_URL)
        with self.assertRaises(ValueError):
            Settings(_env_file=None, DB_TYPE="postgres")
        with self.assertRaises(ValueError):
            Settings(_env_file=None, SQLITE_PATH=":memory:")

    def test_historical_generation_two_signature_stays_immutable(self):
        self.assertEqual(
            schema_migrations.MIGRATIONS[2].checksum,
            "aee46be69d8c5b005cbee58ebfd37feb41d3431eb1fb23bb8339c9076147c03e",
        )

    def test_retired_environment_and_dotenv_fields_cannot_be_silently_ignored(self):
        with patch.dict("os.environ", {"HTTPS_PORT": "443"}):
            with self.assertRaisesRegex(ValueError, "retired"):
                Settings(_env_file=None)
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("SSL_CERT_PATH=/old/certificate.pem\n")
            with self.assertRaisesRegex(ValueError, "SSL_CERT_PATH is retired"):
                Settings(_env_file=env)


class SQLiteDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.file = self.root / "persistent" / "frontiercloud.db"
        self.database = create_sqlite_engine(URL.create("sqlite+aiosqlite", database=str(self.file)))
        self.engine_patch = patch.object(db, "engine", self.database)
        self.secret_patch = patch.object(db, "SECRET_DIR", self.root / "secrets")
        self.engine_patch.start()
        self.secret_patch.start()

    async def asyncTearDown(self):
        self.engine_patch.stop()
        self.secret_patch.stop()
        await self.database.dispose()
        self.temporary.cleanup()

    async def test_real_admin_security_route_sorts_and_pages_ipv4_ipv6_numerically(self):
        from fastapi import FastAPI
        import httpx
        from app.api.v1 import admin
        from app.services import ip_security
        await db.init_db()
        addresses = ["13.11.0.1", "10.199.0.1", "2.0.0.1", "2001:db8::10", "2001:db8::2"]
        async with self.database.begin() as conn:
            for address in addresses:
                await conn.execute(text("INSERT INTO ip_security_summary(ip_address,attack_count,last_attack_at) VALUES(:ip,1,:now)"), {"ip": address, "now": datetime.utcnow()})
        app = FastAPI()
        app.include_router(admin.router, prefix="/admin")
        app.dependency_overrides[admin.require_session] = lambda: "authorized-test-admin"
        with patch.object(ip_security, "engine", self.database):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin.4399.com") as client:
                response = await client.get("/admin/security/blocks", params={"page_size": 3})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item["ip"] for item in response.json()["events"]], ["2.0.0.1", "10.199.0.1", "13.11.0.1"])
                response = await client.get("/admin/security/blocks", params={"page_size": 3, "page": 2})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item["ip"] for item in response.json()["events"]], ["2001:db8::2", "2001:db8::10"])
                response = await client.get("/admin/security/blocks", params={"ip_order": "desc"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item["ip"] for item in response.json()["events"]], ["2001:db8::10", "2001:db8::2", "13.11.0.1", "10.199.0.1", "2.0.0.1"])
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT hex(INET6_ATON('198.51.010.1'))")), "C6330A01")
            self.assertEqual(await conn.scalar(text("SELECT length(INET6_ATON('::ffff:198.51.010.1'))")), 16)
            for address in (None, "fe80::3%1", "198.51.100.0/24", "198.51.1", "198.0xa0.1.2", "198.51.100.2:8080"):
                self.assertIsNone(await conn.scalar(text("SELECT INET6_ATON(:ip)"), {"ip": address}))

    async def _legacy(self, generation=2):
        await db.init_db()
        async with self.database.begin() as conn:
            await conn.execute(text("DROP TABLE media_encryption"))
            await conn.execute(text("DROP TABLE media_crypto_keys"))
            await conn.execute(text("DROP TABLE cluster_upload_sessions"))
            await conn.execute(text("""CREATE TABLE cluster_upload_sessions (
                upload_id TEXT PRIMARY KEY NOT NULL,storage_member_id TEXT NOT NULL,
                media_id TEXT NOT NULL,media_path TEXT NOT NULL,path_locator TEXT UNIQUE,
                object_kind TEXT NOT NULL,expected_bytes BIGINT NOT NULL,state TEXT NOT NULL,
                expires_at BIGINT NOT NULL,created_at BIGINT NOT NULL,updated_at BIGINT NOT NULL)"""))
            await conn.execute(text("INSERT INTO cluster_upload_sessions VALUES (:id,:member,:media,'music/历史/legacy.mp3','legacy-locator','audio',17,'reserved',1,1,1)"), {"id": "e" * 32, "member": "f" * 32, "media": "e" * 64})
            await conn.execute(text("DROP TABLE media_playback_stats"))
            legacy = next(sql for sql in LOCAL_DDL if sql.startswith("CREATE TABLE IF NOT EXISTS media_playback_stats"))
            await conn.execute(text(legacy.replace("BETWEEN -7 AND 500000", "BETWEEN -7 AND 500")))
            for preference in (-7, 100, 500):
                await conn.execute(text("INSERT INTO media_playback_stats VALUES (:id,:path,:score,:preference,:created,:updated)"), {
                    "id": str(preference), "path": f"music/历史/{preference}.mp3", "score": 123456789,
                    "preference": preference, "created": datetime(2026, 1, 2, 3, 4, 5, 123456), "updated": datetime(2026, 2, 3, 4, 5, 6, 654321),
                })
            await conn.execute(text("UPDATE frontiercloud_schema SET generation=:generation"), {"generation": generation})
            if generation == 1:
                await conn.execute(text("DROP TABLE frontiercloud_schema_migrations"))
        return await self._stats()

    async def _stats(self):
        async with self.database.connect() as conn:
            return list((await conn.execute(text("SELECT * FROM media_playback_stats ORDER BY media_id"))).tuples())

    async def test_fresh_bootstrap_and_reopen_preserve_all_tables(self):
        await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await db._table_names(conn), db._required_tables())
            self.assertEqual(await conn.scalar(text("PRAGMA journal_mode")), "wal")
            self.assertEqual(await conn.scalar(text("PRAGMA foreign_keys")), 1)
            self.assertEqual(await conn.scalar(text("PRAGMA integrity_check")), "ok")
            self.assertEqual(await conn.scalar(text("SELECT generation FROM frontiercloud_schema")), schema_migrations.CURRENT_SCHEMA_GENERATION)
        await self.database.dispose()
        await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM ip_security_projection")), 1)

    async def test_marked_generations_one_and_two_upgrade_without_business_rewrite(self):
        for generation in (1, 2):
            with self.subTest(generation=generation):
                if self.file.exists():
                    await self.database.dispose()
                    self.file.unlink()
                before = await self._legacy(generation)
                await db.init_db()
                self.assertEqual(await self._stats(), before)
                async with self.database.begin() as conn:
                    await conn.execute(text("UPDATE media_playback_stats SET preference=500000 WHERE media_id='500'"))
                    self.assertEqual(await conn.scalar(text("SELECT generation FROM frontiercloud_schema")), schema_migrations.CURRENT_SCHEMA_GENERATION)
                    history = list((await conn.execute(text("SELECT generation FROM frontiercloud_schema_migrations ORDER BY generation"))).scalars())
                    self.assertEqual(history, list(range(generation + 1, schema_migrations.CURRENT_SCHEMA_GENERATION+1)))
                    self.assertTrue(await schema_migrations.column_exists(conn, "cluster_upload_sessions", "content_encrypted"))
                    self.assertEqual(await conn.scalar(text("SELECT content_encrypted FROM cluster_upload_sessions")), 0)
                    self.assertEqual(await conn.scalar(text("SELECT expected_bytes FROM cluster_upload_sessions")), 17)
                with self.assertRaises(IntegrityError):
                    async with self.database.begin() as conn:
                        await conn.execute(text("UPDATE media_playback_stats SET preference=500001"))

    async def test_failed_sqlite_rebuild_rolls_back_table_and_generation(self):
        before = await self._legacy()
        original = schema_migrations.MIGRATIONS[3]
        async def fail_after_rebuild(conn):
            await original.apply(conn)
            raise RuntimeError("interrupted before generation commit")
        failing = schema_migrations.SchemaMigration(3, original.name, original.signature, fail_after_rebuild)
        with patch.dict(schema_migrations.MIGRATIONS, {3: failing}):
            with self.assertRaisesRegex(RuntimeError, "generation marker 未推进"):
                await db.init_db()
        self.assertEqual(await self._stats(), before)
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT generation FROM frontiercloud_schema")), 2)
            self.assertNotIn("_fc_priority_generation2", await db._table_names(conn))
        await db.init_db()
        self.assertEqual(await self._stats(), before)

    async def test_concurrent_migration_is_serial_and_idempotent(self):
        before = await self._legacy()
        other = create_sqlite_engine(URL.create("sqlite+aiosqlite", database=str(self.file)))
        async def migrate(database):
            async with database.connect() as conn:
                return await schema_migrations.migrate_schema(conn, 2)
        try:
            self.assertEqual(await asyncio.gather(migrate(self.database), migrate(other)),
                             [schema_migrations.CURRENT_SCHEMA_GENERATION]*2)
        finally:
            await other.dispose()
        self.assertEqual(await self._stats(), before)
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM frontiercloud_schema_migrations")),
                             schema_migrations.CURRENT_SCHEMA_GENERATION-2)

    async def test_unmarked_and_future_database_fail_without_schema_changes(self):
        self.file.parent.mkdir()
        async with self.database.begin() as conn:
            await conn.execute(text("CREATE TABLE external_data (value TEXT)"))
            await conn.execute(text("INSERT INTO external_data VALUES ('untouched')"))
        with self.assertRaisesRegex(RuntimeError, "无代际标记"):
            await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await db._table_names(conn), {"external_data"})
            await conn.execute(text("DROP TABLE external_data"))
            await conn.commit()
        await db.init_db()
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE frontiercloud_schema SET generation=999"))
        with self.assertRaisesRegex(RuntimeError, "禁止旧版本"):
            await db.init_db()

    async def test_missing_sqlite_with_legacy_node_identity_fails_closed(self):
        secrets = self.root / "secrets"
        secrets.mkdir()
        (secrets / "node-vault.key").write_text("existing-node-identity")
        with self.assertRaisesRegex(RuntimeError, "DB_TYPE=mysql"):
            await db.init_db()
        self.assertFalse(self.file.exists())
        self.file.parent.mkdir()
        async with self.database.connect() as conn:
            await conn.scalar(text("PRAGMA user_version"))
        with self.assertRaisesRegex(RuntimeError, "DB_TYPE=mysql"):
            await db.init_db()

    async def test_migration_history_checksum_corruption_is_rejected(self):
        await self._legacy()
        await db.init_db()
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE frontiercloud_schema_migrations SET checksum='corrupt' WHERE generation=3"))
        with self.assertRaisesRegex(RuntimeError, "迁移日志签名不匹配"):
            await db.init_db()

    async def test_native_timestamp_upsert_duplicate_and_exact_path_semantics(self):
        await db.init_db()
        now = datetime(2026, 1, 2, 3, 4, 5, 123456)
        async with self.database.begin() as conn:
            sql = text("INSERT INTO media_visibility(relative_path,hidden,updated_at) VALUES (:path,1,:now) ON DUPLICATE KEY UPDATE hidden=1,updated_at=VALUES(updated_at)")
            for path in ("music/中文/A", "music/中文/a"):
                await conn.execute(sql, {"path": path, "now": now})
            await conn.execute(sql, {"path": "music/中文/A", "now": now})
            result = await conn.scalar(text("SELECT updated_at FROM media_visibility WHERE BINARY relative_path=BINARY :path FOR UPDATE"), {"path": "music/中文/A"})
            self.assertEqual(result, now)
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM media_visibility")), 2)
            self.assertEqual(await conn.scalar(text("SELECT LEFT(:path,CHAR_LENGTH(:prefix))=:prefix"), {"path": "music/中文/a", "prefix": "music/中文"}), 1)
            await conn.execute(text("INSERT IGNORE INTO media_playback_events VALUES ('s','m',:now,:now)"), {"now": now})
            result = await conn.execute(text("INSERT IGNORE INTO media_playback_events VALUES ('s','m',:now,:now)"), {"now": now})
            self.assertEqual(result.rowcount, 0)

    async def test_wal_reader_allows_nested_lookup_and_parallel_writer(self):
        await db.init_db()
        async with self.database.connect() as reader:
            self.assertEqual(await reader.scalar(text("SELECT generation FROM frontiercloud_schema")), schema_migrations.CURRENT_SCHEMA_GENERATION)
            async with asyncio.timeout(2):
                async with self.database.begin() as writer:
                    await writer.execute(text("UPDATE ip_security_projection SET generation=42"))
                    async with self.database.connect() as nested_reader:
                        self.assertEqual(await nested_reader.scalar(text("SELECT generation FROM frontiercloud_schema")), schema_migrations.CURRENT_SCHEMA_GENERATION)
            # The reader retains its pre-write snapshot, and a fresh reader
            # sees the durable update. Neither read reserved a writer lock.
            self.assertEqual(await reader.scalar(text("SELECT generation FROM ip_security_projection")), 0)
        async with self.database.connect() as reader:
            self.assertEqual(await reader.scalar(text("SELECT generation FROM ip_security_projection")), 42)

    async def test_real_webrtc_observation_upsert_and_aggregate_timestamps(self):
        from starlette.requests import Request
        from app.services import network_observation
        await db.init_db()
        request = Request({"type": "http", "client": ("198.51.100.7", 50000), "headers": []})
        with patch.object(network_observation, "engine", self.database), patch.object(network_observation.redis_client, "set", new=AsyncMock(return_value=True)):
            for _ in range(2):
                await network_observation.record_observation(request, ["198.51.100.7"], None)
            pairs = await network_observation.list_observation_summary()
            self.assertEqual(pairs["items"][0]["observation_count"], 2)
            self.assertEqual(pairs["items"][0]["matching_count"], 2)
            self.assertTrue(pairs["items"][0]["last_seen"].endswith("Z"))
            grouped = await network_observation.list_grouped_observation_summary("public")
            self.assertEqual(grouped["groups"][0]["observation_count"], 2)
            self.assertTrue(grouped["groups"][0]["first_seen"].endswith("Z"))

    async def test_real_ip_security_audit_and_datetime_aggregate(self):
        from app.services import ip_security
        await db.init_db()
        async with self.database.begin() as conn:
            for _ in range(2):
                await ip_security._lock_ip_state(conn, "198.51.100.7")
                await ip_security._audit_ip(conn, "198.51.100.7", "invalid_api", {"path": "/api/invalid"})
            self.assertEqual(await conn.scalar(text("SELECT attack_count FROM ip_security_summary")), 2)
            earliest = await conn.scalar(text("SELECT MIN(created_at) FROM ip_security_audit_log"))
            self.assertIsInstance(earliest, datetime)
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM ip_security_locks")), 1)

    async def test_ignore_duplicate_does_not_hide_constraint_errors_and_bounded_cleanup(self):
        await db.init_db()
        with self.assertRaises(IntegrityError):
            async with self.database.begin() as conn:
                await conn.execute(text("INSERT IGNORE INTO media_playback_stats(media_id,media_path,preference,created_at,updated_at) VALUES ('invalid','p',500001,:now,:now)"), {"now": datetime(2026, 1, 2)})
        async with self.database.begin() as conn:
            for i in range(3):
                await conn.execute(text("INSERT INTO media_playback_events VALUES (:id,'m',:now,:now)"), {"id": str(i), "now": datetime(2026, 1, 2)})
            result = await conn.execute(text("DELETE FROM media_playback_events WHERE expires_at <= :now LIMIT 2"), {"now": datetime(2026, 1, 3)})
            self.assertEqual(result.rowcount, 2)
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM media_playback_events")), 1)

    async def test_karaoke_autoincrement_and_json_core_types(self):
        from app.services.karaoke_schema import audit
        await db.init_db()
        async with self.database.begin() as conn:
            for i in range(2):
                await conn.execute(insert(audit).values(user_id=None, action="test", result="success", client_ip="127.0.0.1", webrtc_addresses=["127.0.0.1"], detail={"index": i}, created_at=1))
            from sqlalchemy import select
            rows = list((await conn.execute(select(audit.c.id, audit.c.detail))).tuples())
            self.assertEqual(rows, [(1, {"index": 0}), (2, {"index": 1})])

    async def test_real_playback_service_deduplicates_count_and_persists_priority(self):
        from app.services import playback, media_objects
        await db.init_db()
        media = self.root / "media"
        path = media / "music" / "中文" / "sample.mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"test fixture media bytes")
        with patch.object(playback, "engine", self.database):
            first = await playback.record_playback(media, "music/中文/sample.mp3", "12345678-1234-1234-1234-123456789012", 30, 60)
            second = await playback.record_playback(media, "music/中文/sample.mp3", "12345678-1234-1234-1234-123456789012", 30, 60)
            self.assertTrue(first["counted"])
            self.assertFalse(second["counted"])
            result = await playback.set_preference(media, "music/中文/sample.mp3", 500000)
            self.assertEqual(result["preference"], 500000)
            self.assertEqual(result["play_score"], 1)
        async with self.database.begin() as conn:
            self.assertEqual(await media_objects.ensure_object(conn, "music/中文/sample.mp3", "audio"), first["media_id"])


if __name__ == "__main__":
    unittest.main()
