"""Formal generation-6 rows survive the separate pause-constraint migration."""
import hashlib
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from cryptography.fernet import Fernet
from sqlalchemy import insert,text

from app.core import db,schema_migrations
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_crypto,resource_migration_schema as s,resource_pause_schema as pause
from app.services import node_retirement_schema as retirement
from app.services.federation.state import State
from app.services.s3_members import Members
from app.services.s3_signing import Credentials,Location


class PauseConstraintParserTests(unittest.TestCase):
    def test_native_mysql_formatting_and_redundant_groups_preserve_precedence(self):
        alternate=re.sub(r'\b(state|resume_state|pause_reason)\b',lambda match:'`'+match[0]+'`',pause.CHECK)
        alternate=alternate.replace("'paused'","_utf8mb4'paused'")
        self.assertEqual(pause.canonical('('+alternate+')'),pause.canonical(pause.CHECK))
        self.assertNotEqual(pause.canonical(pause.OLD_CHECK),pause.canonical(pause.CHECK))
        self.assertNotEqual(pause.canonical("state='paused' OR resume_state='' AND pause_reason=''"),pause.canonical(pause.CHECK))
        for unsupported in (pause.CHECK+' -- ignored',pause.CHECK+' OR 1=1',"node_id='x'",''):
            with self.subTest(unsupported=unsupported),self.assertRaises(RuntimeError):pause.canonical(unsupported)

    def test_existing_generation_signatures_remain_exactly_unchanged(self):
        self.assertGreaterEqual(schema_migrations.CURRENT_SCHEMA_GENERATION,7)
        for generation,checksum in {4:'2127eaeaa30788d5725035cd94888849488eb65f1adb9634e673f1dd444dfbef',
                5:'066c5d8d3766fbba3cbf9bf8b529f2b1f8093439b749cc9a91f24e41c5c5610c',
                6:'fbaf9c79e57deac311111f8ec0d3bbcad577c9e99581271c20ada3596eef46ba'}.items():
            self.assertEqual(schema_migrations.MIGRATIONS[generation].checksum,checksum)


class PauseSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.database=create_sqlite_engine('sqlite+aiosqlite:///'+str(self.root/'state.db'))
        self.patches=[patch.object(db,'engine',self.database),patch.object(db,'SECRET_DIR',self.root/'secrets')]
        for item in self.patches:item.start()
        # These are the original generation-5/6 table definitions, not an old
        # version claimed by changing a marker on an already upgraded table.
        with (patch.object(db,'SCHEMA_GENERATION',6),patch.object(pause,'apply',new=AsyncMock()),
                patch.object(pause,'validate',new=AsyncMock()),patch.object(retirement,'create',new=AsyncMock()),
                patch.object(retirement,'validate',new=AsyncMock()),
                patch.object(db,'_required_tables',return_value=db._required_tables()-{'node_retirements'})):
            await db.init_db()
        async with self.database.begin() as conn:
            self.assertEqual(await pause.current(conn),pause.canonical(pause.OLD_CHECK))
        self.store=State(self.database,Fernet.generate_key());await self.store.initialize()
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE node_identity SET role='Master'"))
            for index,state in enumerate(('queued','paused','failed'),1):
                await conn.execute(insert(s.tasks).values(task_id=f'{index:032x}',master_id=self.store.node['node_id'],
                    source_id='a'*32,target_id='b'*32,source_relationship='c'*32,target_relationship='d'*32,
                    plan_sha256='e'*64,plan={'unicode':'历史任务','opaque':['unchanged',None,False,2**53-1]},
                    state=state,resume_state='verifying' if state=='paused' else '',
                    pause_reason='manual' if state=='paused' else '',object_count=3,total_bytes=123,
                    committed_bytes=0,revision=17,created_at=3,updated_at=4))
            await media_crypto.put_encryption(conn,'f'*64,media_crypto.Metadata.new(123))
        self.store.node['role']='Master'
        await Members(self.store).add('old-six',Location('private-fixture-bucket','us-east-1',prefix='managed'),
            Credentials('fixtureAccessKey123','fixture-secret-not-for-logs'),1024**3,'test')
        self.original=self.root/'media/music/original.mp3';self.original.parent.mkdir(parents=True)
        self.original.write_bytes(b'unchanged original media'*4096)
        self.original_hash=hashlib.sha256(self.original.read_bytes()).hexdigest()
        self.before=await self.snapshot()

    async def asyncTearDown(self):
        for item in reversed(self.patches):item.stop()
        await self.database.dispose();self.temporary.cleanup()

    async def snapshot(self):
        async with self.database.connect() as conn:
            tables=await db._table_names(conn)
            return {name:list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples())
                for name in sorted(tables-{'frontiercloud_schema','frontiercloud_schema_migrations','node_retirements'})}

    async def preserved(self):
        self.assertEqual(await self.snapshot(),self.before)
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(),self.original_hash)

    async def test_actual_upgrade_noop_restart_and_all_prior_values_are_preserved(self):
        await db.init_db();await self.preserved();await db.init_db();await self.preserved()
        async with self.database.connect() as conn:
            await pause.validate(conn)
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),schema_migrations.CURRENT_SCHEMA_GENERATION)
            self.assertEqual(await conn.scalar(text('SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=7')),
                schema_migrations.MIGRATIONS[7].checksum)

    async def interrupted(self,statement):
        original=schema_migrations.execute_ddl
        async def fail(conn,sql):
            await original(conn,sql)
            if sql==statement:raise RuntimeError('controlled post-DDL failure')
        with patch.object(schema_migrations,'execute_ddl',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'marker 未推进'):await db.init_db()
        await self.preserved()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),6)
            self.assertEqual(await pause.current(conn),pause.canonical(pause.OLD_CHECK))
            self.assertFalse(await schema_migrations.table_exists(conn,'migration_tasks_pause_v7'))
        await db.init_db();await self.preserved()

    async def test_sqlite_post_drop_failure_rolls_back_the_entire_table_swap(self):
        await self.interrupted('DROP TABLE migration_tasks')

    async def test_sqlite_post_rename_failure_restores_original_rows_and_indexes(self):
        await self.interrupted('ALTER TABLE migration_tasks_pause_v7 RENAME TO migration_tasks')

    async def test_unknown_temporary_table_is_never_removed_to_force_an_upgrade(self):
        async with self.database.begin() as conn:
            await conn.execute(text('CREATE TABLE migration_tasks_pause_v7 (evidence TEXT NOT NULL)'))
            await conn.execute(text("INSERT INTO migration_tasks_pause_v7 VALUES ('keep this foreign table')"))
        with self.assertRaisesRegex(RuntimeError,'marker 未推进'):await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT evidence FROM migration_tasks_pause_v7')),'keep this foreign table')
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),6)
        actual=await self.snapshot();actual.pop('migration_tasks_pause_v7')
        self.assertEqual(actual,self.before)

    async def test_unknown_task_trigger_is_never_discarded_by_the_table_rebuild(self):
        async with self.database.begin() as conn:
            await conn.execute(text('CREATE TRIGGER keep_unknown_task_trigger AFTER UPDATE ON migration_tasks BEGIN SELECT 1; END'))
        with self.assertRaisesRegex(RuntimeError,'marker 未推进'):await db.init_db()
        await self.preserved()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM sqlite_master WHERE name='keep_unknown_task_trigger'")),1)
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),6)

    async def test_unknown_task_index_is_never_silently_removed(self):
        async with self.database.begin() as conn:
            await conn.execute(text('CREATE INDEX keep_unknown_task_index ON migration_tasks(revision)'))
        with self.assertRaisesRegex(RuntimeError,'marker 未推进'):await db.init_db()
        await self.preserved()
        async with self.database.connect() as conn:
            self.assertTrue(await schema_migrations.index_exists(conn,'migration_tasks','keep_unknown_task_index'))
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),6)

    async def test_foreign_child_rows_cannot_be_cascaded_away_by_the_table_swap(self):
        async with self.database.begin() as conn:
            await conn.execute(text('CREATE TABLE foreign_task_child (task_id TEXT REFERENCES migration_tasks(task_id) ON DELETE CASCADE)'))
            await conn.execute(text('INSERT INTO foreign_task_child VALUES (:task)'),{'task':f'{1:032x}'})
        with self.assertRaisesRegex(RuntimeError,'marker 未推进'):await db.init_db()
        actual=await self.snapshot();self.assertEqual(actual.pop('foreign_task_child'),[(f'{1:032x}',)])
        self.assertEqual(actual,self.before)
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),6)
