"""Real MySQL 8.4 upgrade checks, restricted to the disposable CI database."""
import os
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db, schema_migrations


@unittest.skipUnless(os.environ.get("FC_DB_MYSQL_RUNTIME") == "1", "requires disposable MySQL integration fixture")
class MySQLDatabaseRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = make_url(os.environ["FC_DB_MYSQL_URL"])
        if url.host != "mysql" or url.database != "frontiercloud_db_test":
            raise RuntimeError("Refusing MySQL integration tests outside disposable mysql/frontiercloud_db_test")
        self.database = create_async_engine(url, pool_pre_ping=True)
        self.patch = patch.object(db, "engine", self.database)
        self.patch.start()
        async with self.database.connect() as conn:
            for name in await db._table_names(conn):
                if not name.replace("_", "").isalnum():
                    raise RuntimeError("unsafe fixture table")
                await conn.execute(text(f"DROP TABLE `{name}`"))
                await conn.commit()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.database.dispose()

    async def test_fresh_mysql_bootstrap_current_schema_and_noop_restart(self):
        await db.init_db()
        await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await db._table_names(conn), db._required_tables())
            self.assertEqual(await conn.scalar(text("SELECT generation FROM frontiercloud_schema")), 4)
            kind = await conn.scalar(text("SELECT COLUMN_TYPE FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='media_playback_stats' AND column_name='preference'"))
            self.assertEqual(kind, "int")

    async def test_marked_mysql_generation_one_widens_priorities_preserving_every_row(self):
        await db.init_db()
        async with self.database.connect() as conn:
            await conn.execute(text("DROP TABLE media_encryption"))
            await conn.execute(text("DROP TABLE media_crypto_keys"))
            await conn.execute(text("ALTER TABLE cluster_upload_sessions DROP CHECK chk_upload_encryption_mode"))
            await conn.execute(text("ALTER TABLE cluster_upload_sessions DROP COLUMN content_encrypted"))
            await conn.execute(text("DROP TABLE frontiercloud_schema_migrations"))
            await conn.execute(text("ALTER TABLE media_playback_stats DROP CHECK chk_media_preference"))
            await conn.execute(text("ALTER TABLE media_playback_stats MODIFY preference SMALLINT NOT NULL DEFAULT 0"))
            await conn.execute(text("ALTER TABLE media_playback_stats ADD CONSTRAINT chk_media_preference CHECK (preference BETWEEN -7 AND 500)"))
            await conn.execute(text("UPDATE frontiercloud_schema SET generation=1"))
            now = datetime(2026, 1, 2, 3, 4, 5, 123456)
            for value in (-7, 100, 500):
                await conn.execute(text("INSERT INTO media_playback_stats VALUES (:id,:path,123456789,:preference,:now,:now)"), {"id": str(value), "path": f"music/历史/{value}.mp3", "preference": value, "now": now})
            await conn.commit()
            before = list((await conn.execute(text("SELECT * FROM media_playback_stats ORDER BY media_id"))).tuples())
        await db.init_db()
        async with self.database.begin() as conn:
            after = list((await conn.execute(text("SELECT * FROM media_playback_stats ORDER BY media_id"))).tuples())
            self.assertEqual(before, after)
            self.assertEqual(await conn.scalar(text("SELECT generation FROM frontiercloud_schema")), 4)
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM frontiercloud_schema_migrations")), 3)
            self.assertTrue(await schema_migrations.column_exists(conn, "cluster_upload_sessions", "content_encrypted"))
            self.assertEqual(await conn.scalar(text("SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=2")), "aee46be69d8c5b005cbee58ebfd37feb41d3431eb1fb23bb8339c9076147c03e")
            await conn.execute(text("UPDATE media_playback_stats SET preference=500000 WHERE media_id='500'"))
        await db.init_db()

    async def test_mysql_interrupted_generation_three_retries_after_implicit_ddl_commit(self):
        await db.init_db()
        async with self.database.connect() as conn:
            await conn.execute(text("UPDATE frontiercloud_schema SET generation=2"))
            await conn.commit()
        original = schema_migrations.MIGRATIONS[3]
        async def fail_after_ddl(conn):
            await original.apply(conn)
            raise RuntimeError("interrupted MySQL DDL")
        failing = schema_migrations.SchemaMigration(3, original.name, original.signature, fail_after_ddl)
        with patch.dict(schema_migrations.MIGRATIONS, {3: failing}):
            with self.assertRaisesRegex(RuntimeError, "generation marker 未推进"):
                await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT generation FROM frontiercloud_schema")), 2)
        await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT generation FROM frontiercloud_schema")), 4)

    async def test_mysql_future_and_unmarked_databases_are_rejected(self):
        await db.init_db()
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE frontiercloud_schema SET generation=999"))
        with self.assertRaisesRegex(RuntimeError, "禁止旧版本"):
            await db.init_db()
        async with self.database.connect() as conn:
            await conn.execute(text("DROP TABLE frontiercloud_schema"))
            await conn.commit()
        with self.assertRaisesRegex(RuntimeError, "无代际标记"):
            await db.init_db()


if __name__ == "__main__":
    unittest.main()
