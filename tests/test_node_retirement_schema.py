"""Generation-7 upgrade preservation and fail-closed retirement authority DDL."""
import hashlib
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from cryptography.fernet import Fernet
from sqlalchemy import CheckConstraint,insert,text
from sqlalchemy.exc import IntegrityError

from app.core import db,schema_migrations
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_crypto,node_retirement_schema as s
from app.services.federation.state import State


class RetirementCheckTests(unittest.TestCase):
    def test_native_mysql_expression_format_preserves_boolean_precedence(self):
        for check in (value for value in s.retirements.constraints if isinstance(value,CheckConstraint)):
            source=str(check.sqltext)
            alternate=re.sub(r'\b(side|phase|revision|started_at|completed_at)\b',lambda match:'`'+match[0]+'`',source)
            alternate=alternate.replace("'retiring'","_utf8mb4'retiring'")
            self.assertEqual(s.canonical_check(source),s.canonical_check('('+alternate+')'))
        completion=next(str(value.sqltext) for value in s.retirements.constraints if value.name=='chk_node_retirement_completion')
        self.assertNotEqual(s.canonical_check(completion),s.canonical_check("phase='retiring' AND (completed_at=0 OR phase='completed') AND completed_at>=started_at"))
        for invalid in ('','phase =',"phase='completed' -- ignored",'unknown_column=0'):
            with self.subTest(invalid=invalid),self.assertRaises(RuntimeError):s.canonical_check(invalid)

    def test_current_generation_is_nine_and_retirement_migration_signatures_are_unchanged(self):
        self.assertEqual(schema_migrations.CURRENT_SCHEMA_GENERATION,9)
        for generation,checksum in {4:'2127eaeaa30788d5725035cd94888849488eb65f1adb9634e673f1dd444dfbef',
                5:'066c5d8d3766fbba3cbf9bf8b529f2b1f8093439b749cc9a91f24e41c5c5610c',
                6:'fbaf9c79e57deac311111f8ec0d3bbcad577c9e99581271c20ada3596eef46ba',
                7:'9879c05d0db2e99166962d7d05908b6ee8e44c392d7392ab9993d132f9b85dd0'}.items():
            self.assertEqual(schema_migrations.MIGRATIONS[generation].checksum,checksum)
        self.assertEqual(schema_migrations.MIGRATIONS[8].checksum,'3b1db26eaf26b408018a5418cf3cdc403f13fd837069b8fd404e704a0ba7f92f')


class RetirementSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.database=create_sqlite_engine('sqlite+aiosqlite:///'+str(self.root/'state.db'))
        self.patches=[patch.object(db,'engine',self.database),patch.object(db,'SECRET_DIR',self.root/'secrets')]
        for item in self.patches:item.start()
        required=db._required_tables()-{'node_retirements'}
        # Build the actual previous schema; no retirement table is fabricated.
        with patch.object(db,'SCHEMA_GENERATION',7),patch.object(db,'_required_tables',return_value=required),patch.object(s,'create',new=AsyncMock()),patch.object(s,'validate',new=AsyncMock()):
            await db.init_db()
        self.store=State(self.database,Fernet.generate_key());await self.store.initialize()
        async with self.database.begin() as conn:
            await media_crypto.put_encryption(conn,'a'*64,media_crypto.Metadata.new(123))
            self.assertFalse(await schema_migrations.table_exists(conn,'node_retirements'))
        self.original=self.root/'original.mp3';self.original.write_bytes(b'unchanged original'*4096)
        self.original_hash=hashlib.sha256(self.original.read_bytes()).hexdigest();self.before=await self.snapshot()

    async def asyncTearDown(self):
        for item in reversed(self.patches):item.stop()
        await self.database.dispose();self.temporary.cleanup()

    async def snapshot(self):
        async with self.database.connect() as conn:
            tables=await db._table_names(conn)
            return {name:list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples()) for name in sorted(tables-{'frontiercloud_schema','frontiercloud_schema_migrations','node_retirements'})}

    async def preserved(self):
        self.assertEqual(await self.snapshot(),self.before)
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(),self.original_hash)
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT private_key FROM node_identity')),self.store.node['private_key'])

    async def test_upgrade_restart_and_retry_preserve_every_existing_row_key_and_media(self):
        await db.init_db();await self.preserved();await db.init_db();await self.preserved()
        async with self.database.connect() as conn:
            await s.validate(conn)
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),schema_migrations.CURRENT_SCHEMA_GENERATION)
            self.assertEqual(await conn.scalar(text('SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=8')),schema_migrations.MIGRATIONS[8].checksum)
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM node_retirements')),0)

    async def test_failure_after_create_does_not_advance_generation_and_retry_is_safe(self):
        original=s.create
        async def fail(conn):
            await original(conn);raise RuntimeError('controlled retirement DDL interruption')
        with patch.object(s,'create',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'marker 未推进'):await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),7)
            self.assertFalse(await schema_migrations.table_exists(conn,'node_retirements'))
        await self.preserved();await db.init_db();await self.preserved()

    async def test_unknown_same_name_table_is_retained_and_upgrade_refused(self):
        async with self.database.begin() as conn:
            await conn.execute(text('CREATE TABLE node_retirements (keep_evidence TEXT NOT NULL)'))
            await conn.execute(text("INSERT INTO node_retirements VALUES ('foreign data')"))
        with self.assertRaisesRegex(RuntimeError,'marker 未推进'):await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),7)
            self.assertEqual(await conn.scalar(text('SELECT keep_evidence FROM node_retirements')),'foreign data')
        await self.preserved()

    async def test_unknown_trigger_is_not_discarded_or_admitted(self):
        await db.init_db()
        async with self.database.begin() as conn:
            await conn.execute(text('CREATE TRIGGER foreign_retirement_trigger AFTER INSERT ON node_retirements BEGIN SELECT 1; END'))
        with self.assertRaisesRegex(RuntimeError,'Unknown retirement journal trigger'):await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM sqlite_master WHERE name='foreign_retirement_trigger'")),1)
        await self.preserved()

    async def test_durable_schema_rejects_bad_phase_completion_and_duplicate_operation(self):
        await db.init_db()
        row=dict(relationship_id='1'*32,retirement_id='2'*32,node_id=self.store.node['node_id'],master_id='3'*32,member_id='4'*32,
            side='master',phase='retiring',binding_sha256='5'*64,authorization_sha256='6'*64,binding_json='{}',roots_json='{}',receipt_json='{}',revision=1,started_at=3,completed_at=0)
        async with self.database.begin() as conn:await conn.execute(insert(s.retirements).values(**row))
        for changes in ({'relationship_id':'7'*32},{'phase':'unknown'},{'phase':'completed','completed_at':2},{'completed_at':4},{'revision':0},{'side':'other'}):
            candidate={**row,**changes}
            if changes!={'relationship_id':'7'*32}:candidate.update(relationship_id='8'*32,retirement_id='9'*32)
            with self.subTest(changes=changes),self.assertRaises(IntegrityError):
                async with self.database.begin() as conn:await conn.execute(insert(s.retirements).values(**candidate))
        async with self.database.connect() as conn:self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM node_retirements')),1)
        await self.preserved()

    async def test_current_schema_with_a_missing_index_refuses_start_until_explicit_repair(self):
        await db.init_db()
        async with self.database.begin() as conn:await conn.execute(text('DROP INDEX idx_node_retirement_member'))
        with self.assertRaisesRegex(RuntimeError,'Retirement journal indexes invalid'):await db.init_db()
        async with self.database.begin() as conn:await s.create(conn)
        await db.init_db();await self.preserved()
