"""Actual generation-8 preservation and cancellation-census DDL recovery."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from sqlalchemy import insert,text
from sqlalchemy.exc import IntegrityError,OperationalError

from app.core import db,schema_migrations as migrations
from app.core.sqlite_compat import create_sqlite_engine
from app.services import resource_cancel_census_schema as s


class CancellationCensusSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.database=create_sqlite_engine('sqlite+aiosqlite:///'+str(self.root/'state.db'))
        self.patches=[patch.object(db,'engine',self.database),patch.object(db,'SECRET_DIR',self.root/'secrets')]
        for item in self.patches:item.start()
        required=db._required_tables
        with (patch.object(db,'SCHEMA_GENERATION',8),patch.object(migrations,'CURRENT_SCHEMA_GENERATION',8),
                patch.object(migrations,'MIGRATIONS',{key:value for key,value in migrations.MIGRATIONS.items() if key<=8}),
                patch.object(s,'create',new=AsyncMock()),patch.object(s,'validate',new=AsyncMock()),
                patch.object(db,'_required_tables',side_effect=lambda:required()-{s.snapshots.name})):
            await db.init_db()
        async with self.database.begin() as conn:
            self.assertFalse(await migrations.table_exists(conn,s.snapshots.name))
            await conn.execute(text("INSERT INTO media_objects VALUES (:id,'audio','music/kept/original.mp3',:locator,'2026-01-01','2026-01-01')"),
                dict(id='a'*64,locator=hashlib.sha256(b'music/kept/original.mp3').hexdigest()))
        self.media=self.root/'original.mp3';self.media.write_bytes(b'kept original bytes'*1000)
        self.before=await self.snapshot();self.media_hash=hashlib.sha256(self.media.read_bytes()).hexdigest()

    async def asyncTearDown(self):
        for item in reversed(self.patches):item.stop()
        await self.database.dispose();self.temp.cleanup()

    async def snapshot(self):
        async with self.database.connect() as conn:
            tables=await db._table_names(conn)
            return {name:list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples()) for name in sorted(
                tables-{'frontiercloud_schema','frontiercloud_schema_migrations',s.snapshots.name})}

    async def preserved(self):
        self.assertEqual(await self.snapshot(),self.before)
        self.assertEqual(hashlib.sha256(self.media.read_bytes()).hexdigest(),self.media_hash)

    async def test_upgrade_and_restart_preserve_every_existing_row_and_original(self):
        await db.init_db();await db.init_db();await self.preserved()
        async with self.database.connect() as conn:
            await s.validate(conn)
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),9)
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM migration_cancel_census')),0)
            self.assertIsNone(await conn.scalar(text('SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=8')))
            self.assertEqual(await conn.scalar(text('SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=9')),migrations.MIGRATIONS[9].checksum)

    async def test_ddl_interruption_keeps_old_generation_and_retry_preserves_rows(self):
        create=s.create
        async def fail(conn):await create(conn);raise RuntimeError('Controlled census DDL interruption')
        with patch.object(s,'create',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'marker 未推进'):await db.init_db()
        async with self.database.connect() as conn:self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),8)
        await self.preserved();await db.init_db();await self.preserved()

    async def test_unknown_same_name_table_is_preserved_and_refuses_upgrade(self):
        async with self.database.begin() as conn:
            await conn.execute(text('CREATE TABLE migration_cancel_census (keep_evidence TEXT NOT NULL)'))
            await conn.execute(text("INSERT INTO migration_cancel_census VALUES ('foreign data')"))
        with self.assertRaisesRegex(RuntimeError,'marker 未推进'):await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),8)
            self.assertEqual(await conn.scalar(text('SELECT keep_evidence FROM migration_cancel_census')),'foreign data')
        await self.preserved()

    async def test_unknown_trigger_is_preserved_and_refuses_current_startup(self):
        await db.init_db()
        async with self.database.begin() as conn:
            if conn.dialect.name=='sqlite':statement='CREATE TRIGGER foreign_census_trigger AFTER INSERT ON migration_cancel_census BEGIN SELECT 1; END'
            else:statement='CREATE TRIGGER foreign_census_trigger BEFORE INSERT ON migration_cancel_census FOR EACH ROW SET NEW.seen=1'
            await conn.execute(text(statement))
        with self.assertRaisesRegex(RuntimeError,'Unknown cancellation census trigger'):await db.init_db()
        await self.preserved()

    async def test_missing_index_is_not_silently_accepted_in_current_generation(self):
        await db.init_db()
        async with self.database.begin() as conn:
            tail='' if conn.dialect.name=='sqlite' else ' ON migration_cancel_census'
            await conn.execute(text('DROP INDEX uq_cancel_census_path'+tail))
        with self.assertRaisesRegex(RuntimeError,'Invalid cancellation census indexes'):await db.init_db()
        await self.preserved()

    async def test_snapshots_reject_duplicate_native_ids_paths_and_invalid_progress(self):
        await db.init_db();row=dict(task_id='b'*32,logical_id='c'*64,census_id='d'*32,resource_kind='media',
            object_id='e'*64,physical_path='music/kept/original.mp3',path_locator='f'*64,expected_bytes=12,native_json='{}',catalog_json='{}',seen=0)
        async with self.database.begin() as conn:await conn.execute(insert(s.snapshots).values(**row))
        for changes in ({'logical_id':'0'*64},{'logical_id':'0'*64,'object_id':'1'*64},
                {'logical_id':'0'*64,'object_id':'1'*64,'path_locator':'2'*64,'seen':2},
                {'logical_id':'0'*64,'object_id':'1'*64,'path_locator':'2'*64,'expected_bytes':0}):
            with self.assertRaises((IntegrityError,OperationalError)) as caught:
                async with self.database.begin() as conn:await conn.execute(insert(s.snapshots).values(**{**row,**changes}))
            if isinstance(caught.exception,OperationalError):
                self.assertEqual(self.database.dialect.name,'mysql');self.assertEqual(caught.exception.orig.args[0],3819)
        await self.preserved()

    async def test_generation_eight_signature_is_unchanged_and_current_is_nine(self):
        self.assertEqual(migrations.CURRENT_SCHEMA_GENERATION,9)
        self.assertEqual(migrations.MIGRATIONS[8].checksum,'3b1db26eaf26b408018a5418cf3cdc403f13fd837069b8fd404e704a0ba7f92f')

    async def test_actual_mysql_lock_name_is_compatible_exclusive_and_released(self):
        if self.database.dialect.name!='mysql':self.skipTest('Requires the guarded actual MySQL fixture')
        async with self.database.connect() as first,self.database.connect() as second:
            database=await first.scalar(text('SELECT DATABASE()'));legacy='frontiercloud:schema-migrations:'+database
            expected=legacy if len(legacy)<=64 else hashlib.sha256(legacy.encode()).hexdigest()
            self.assertEqual(await first.scalar(text('SELECT '+migrations.MYSQL_MIGRATION_LOCK_NAME)),expected)
            await migrations._acquire_lock(first)
            try:
                self.assertEqual(await second.scalar(text('SELECT IS_USED_LOCK(:name)'),dict(name=expected)),await first.scalar(text('SELECT CONNECTION_ID()')))
                with patch.object(migrations,'MIGRATION_LOCK_TIMEOUT_SECONDS',0):
                    with self.assertRaisesRegex(RuntimeError,'迁移锁超时'):await migrations._acquire_lock(second)
            finally:await migrations._release_lock(first)
            self.assertIsNone(await second.scalar(text('SELECT IS_USED_LOCK(:name)'),dict(name=expected)))
            await migrations._acquire_lock(second);await migrations._release_lock(second)
        await self.preserved()
