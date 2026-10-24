"""Real MySQL 8.4 upgrade checks, restricted to the disposable CI database."""
import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock,patch

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
            self.assertEqual(await conn.scalar(text("SELECT generation FROM frontiercloud_schema")), schema_migrations.CURRENT_SCHEMA_GENERATION)
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
            self.assertEqual(await conn.scalar(text("SELECT generation FROM frontiercloud_schema")), schema_migrations.CURRENT_SCHEMA_GENERATION)
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM frontiercloud_schema_migrations")), schema_migrations.CURRENT_SCHEMA_GENERATION-schema_migrations.BASE_SCHEMA_GENERATION)
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
            self.assertEqual(await conn.scalar(text("SELECT generation FROM frontiercloud_schema")), schema_migrations.CURRENT_SCHEMA_GENERATION)

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

    async def test_generation_four_preserves_old_tables_and_retries_partial_ddl(self):
        from cryptography.fernet import Fernet
        from app.services.federation.state import State
        from app.services import resource_migration_schema as s
        await db.init_db()
        store = State(self.database, Fernet.generate_key())
        await store.initialize()
        async with self.database.connect() as conn:
            for table in reversed(s.metadata.sorted_tables):
                await conn.run_sync(table.drop)
                await conn.commit()
            await conn.execute(text('UPDATE frontiercloud_schema SET generation=4'))
            await conn.commit()
            old_tables = await db._table_names(conn)
            before = {name: list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples())
                      for name in old_tables - {'frontiercloud_schema', 'frontiercloud_schema_migrations'}}
        with patch.object(s, 'validate', side_effect=RuntimeError('after implicit DDL commit')):
            with self.assertRaisesRegex(RuntimeError, 'generation marker 未推进'):
                await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')), 4)
            self.assertTrue(set(s.metadata.tables) <= await db._table_names(conn))
            await conn.execute(text('DROP INDEX idx_migration_task_state ON migration_tasks'))
            await conn.commit()
        await db.init_db()
        async with self.database.connect() as conn:
            for name, rows in before.items():
                self.assertEqual(list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples()), rows)
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')), schema_migrations.CURRENT_SCHEMA_GENERATION)
            self.assertTrue(await schema_migrations.index_exists(conn, 'migration_tasks', 'idx_migration_task_state'))
            private = await conn.scalar(text('SELECT private_key FROM node_identity'))
            self.assertTrue(store.unseal(private))

    async def test_generation_five_s3_upgrade_retries_partial_ddl_and_preserves_old_values(self):
        from cryptography.fernet import Fernet
        from app.services.federation.state import State
        from app.services import s3_schema as s
        await db.init_db()
        store = State(self.database,Fernet.generate_key())
        await store.initialize()
        async with self.database.connect() as conn:
            for table in reversed(s.metadata.sorted_tables):
                await conn.run_sync(table.drop)
                await conn.commit()
            await conn.execute(text('UPDATE frontiercloud_schema SET generation=5'))
            await conn.commit()
            tables = await db._table_names(conn)
            before = {name:list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples())
                for name in tables - {'frontiercloud_schema','frontiercloud_schema_migrations'}}
        with patch.object(s,'validate',side_effect=RuntimeError('after implicit S3 DDL commit')):
            with self.assertRaisesRegex(RuntimeError,'generation marker 未推进'):
                await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),5)
            await conn.execute(text('DROP INDEX idx_s3_upload_recovery ON s3_upload_sessions'))
            await conn.commit()
        await db.init_db()
        async with self.database.connect() as conn:
            for name,rows in before.items():
                self.assertEqual(list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples()),rows)
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),schema_migrations.CURRENT_SCHEMA_GENERATION)
            self.assertTrue(await schema_migrations.index_exists(conn,'s3_upload_sessions','idx_s3_upload_recovery'))
            self.assertEqual(await conn.scalar(text('SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=6')),
                schema_migrations.MIGRATIONS[6].checksum)

    async def test_mysql_migration_claims_and_control_fence_old_workers(self):
        import asyncio
        from cryptography.fernet import Fernet
        from sqlalchemy import insert, update
        from app.services.federation import schema as federation
        from app.services.federation.state import State
        from app.services import resource_migration_schema as s
        from app.services.resource_migration_journal import Conflict, Journal
        await db.init_db()
        store = State(self.database, Fernet.generate_key())
        await store.initialize()
        async with self.database.begin() as conn:
            await conn.execute(update(federation.identity).values(role='Master'))
            await conn.execute(insert(s.tasks).values(task_id='a'*32, master_id=store.node['node_id'],
                source_id='b'*32, target_id='c'*32, source_relationship='b'*32, target_relationship='c'*32,
                plan_sha256='f'*64, plan={}, state='queued', object_count=0, total_bytes=0,
                created_at=1, updated_at=1))
        now = [1000]
        journal = Journal(store, clock=lambda: now[0])
        results = await asyncio.gather(journal.claim('a'*32, 'd'*32), journal.claim('a'*32, 'e'*32),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(result, Conflict) for result in results), 1)
        lease = next(result for result in results if not isinstance(result, Exception))
        revision = await journal.control('a'*32, lease.revision, 'pause', 'admin')
        with self.assertRaises(Conflict):
            await journal.renew(lease)
        with self.assertRaises(Conflict):
            await journal.control('a'*32, revision, 'traffic_resume', 'scheduler')
        await journal.control('a'*32, revision, 'resume', 'admin')
        fresh = await journal.claim('a'*32, 'f'*32)
        self.assertGreater(fresh.epoch, lease.epoch)

    async def pause_generation_six(self):
        from cryptography.fernet import Fernet
        from sqlalchemy import insert
        from app.services.federation.state import State
        from app.services import resource_migration_schema as s,resource_pause_schema as pause,media_crypto,node_retirement_schema as retirement
        from app.services.s3_members import Members
        from app.services.s3_signing import Credentials,Location
        # Original definitions and original marker, before the new check is
        # applied. This fixture is not an execution of an old application.
        with (patch.object(db,'SCHEMA_GENERATION',6),patch.object(pause,'apply',new=AsyncMock()),
                patch.object(pause,'validate',new=AsyncMock()),patch.object(retirement,'create',new=AsyncMock()),
                patch.object(retirement,'validate',new=AsyncMock()),
                patch.object(db,'_required_tables',return_value=db._required_tables()-{'node_retirements'})):
            await db.init_db()
        store=State(self.database,Fernet.generate_key());await store.initialize()
        async with self.database.begin() as conn:
            self.assertEqual(await pause.current(conn),pause.canonical(pause.OLD_CHECK))
            await conn.execute(text("UPDATE node_identity SET role='Master'"))
            for index,state in enumerate(('queued','paused','failed'),1):
                await conn.execute(insert(s.tasks).values(task_id=f'{index:032x}',master_id=store.node['node_id'],
                    source_id='a'*32,target_id='b'*32,source_relationship='c'*32,target_relationship='d'*32,
                    plan_sha256='e'*64,plan={'unicode':'历史任务','opaque':[None,False,2**53-1]},
                    state=state,resume_state='verifying' if state=='paused' else '',
                    pause_reason='manual' if state=='paused' else '',object_count=3,total_bytes=123,
                    committed_bytes=0,revision=17,created_at=3,updated_at=4))
            await media_crypto.put_encryption(conn,'f'*64,media_crypto.Metadata.new(123))
        store.node['role']='Master'
        await Members(store).add('old-six',Location('private-fixture-bucket','us-east-1',prefix='managed'),
            Credentials('fixtureAccessKey123','fixture-secret-not-for-logs'),1024**3,'test')
        return await self.pause_snapshot()

    async def pause_snapshot(self):
        async with self.database.connect() as conn:
            return {name:list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples())
                for name in sorted((await db._table_names(conn))-{'frontiercloud_schema','frontiercloud_schema_migrations','node_retirements'})}

    async def test_generation_six_pause_upgrade_preserves_all_native_rows_and_noop_restart(self):
        from app.services import resource_pause_schema as pause
        before=await self.pause_generation_six()
        await db.init_db();self.assertEqual(await self.pause_snapshot(),before)
        await db.init_db();self.assertEqual(await self.pause_snapshot(),before)
        async with self.database.connect() as conn:
            await pause.validate(conn)
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),schema_migrations.CURRENT_SCHEMA_GENERATION)
            self.assertEqual(await conn.scalar(text('SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=7')),
                schema_migrations.MIGRATIONS[7].checksum)

    async def test_generation_six_pause_upgrade_retries_after_implicit_ddl_without_row_rewrite(self):
        from app.services import resource_pause_schema as pause
        before=await self.pause_generation_six();original=schema_migrations.MIGRATIONS[7]
        async def interrupted(conn):
            await original.apply(conn)
            raise RuntimeError('controlled interruption after native MySQL ALTER')
        failing=schema_migrations.SchemaMigration(7,original.name,original.signature,interrupted)
        with patch.dict(schema_migrations.MIGRATIONS,{7:failing}):
            with self.assertRaisesRegex(RuntimeError,'generation marker 未推进'):await db.init_db()
        self.assertEqual(await self.pause_snapshot(),before)
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),6)
            # MySQL has committed the single ALTER, but the marker and proof
            # transaction did not advance. Retry must recognize that check.
            await pause.validate(conn)
            self.assertIsNone(await conn.scalar(text('SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=7')))
        await db.init_db();self.assertEqual(await self.pause_snapshot(),before)

    async def retirement_generation_seven(self):
        from cryptography.fernet import Fernet
        from app.services.federation.state import State
        from app.services import node_retirement_schema as retirement,media_crypto
        with (patch.object(db,'SCHEMA_GENERATION',7),patch.object(retirement,'create',new=AsyncMock()),
                patch.object(retirement,'validate',new=AsyncMock()),
                patch.object(db,'_required_tables',return_value=db._required_tables()-{'node_retirements'})):
            await db.init_db()
        store=State(self.database,Fernet.generate_key());await store.initialize()
        async with self.database.begin() as conn:
            self.assertFalse(await schema_migrations.table_exists(conn,'node_retirements'))
            await media_crypto.put_encryption(conn,'a'*64,media_crypto.Metadata.new(123))
            await conn.execute(text("INSERT INTO media_visibility VALUES ('music/历史/保留',1,'2026-10-20 01:02:03')"))
        return await self.pause_snapshot()

    async def test_generation_seven_retirement_upgrade_and_restart_preserve_all_old_rows(self):
        from app.services import node_retirement_schema as retirement
        before=await self.retirement_generation_seven()
        await db.init_db();self.assertEqual(await self.pause_snapshot(),before)
        await db.init_db();self.assertEqual(await self.pause_snapshot(),before)
        async with self.database.connect() as conn:
            await retirement.validate(conn)
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),schema_migrations.CURRENT_SCHEMA_GENERATION)
            self.assertEqual(await conn.scalar(text('SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=8')),schema_migrations.MIGRATIONS[8].checksum)

    async def test_retirement_mysql_retry_repairs_committed_create_before_indexes(self):
        from app.services import node_retirement_schema as retirement
        before=await self.retirement_generation_seven();original=schema_migrations.execute_ddl
        async def interrupted(conn,statement):
            await original(conn,statement)
            if 'CREATE TABLE IF NOT EXISTS node_retirements' in statement:
                raise RuntimeError('controlled process loss after committed native CREATE')
        with patch.object(schema_migrations,'execute_ddl',side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError,'generation marker 未推进'):await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),7)
            await retirement.validate(conn,require_indexes=False)
            with self.assertRaisesRegex(RuntimeError,'Retirement journal indexes invalid'):await retirement.validate(conn)
        self.assertEqual(await self.pause_snapshot(),before)
        await db.init_db();self.assertEqual(await self.pause_snapshot(),before)

    async def test_retirement_mysql_foreign_table_is_retained_without_generation_advance(self):
        before=await self.retirement_generation_seven()
        async with self.database.connect() as conn:
            await conn.execute(text('CREATE TABLE node_retirements (keep_evidence TEXT NOT NULL)'))
            await conn.execute(text("INSERT INTO node_retirements VALUES ('keep foreign material')"));await conn.commit()
        with self.assertRaisesRegex(RuntimeError,'generation marker 未推进'):await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),7)
            self.assertEqual(await conn.scalar(text('SELECT keep_evidence FROM node_retirements')),'keep foreign material')
        self.assertEqual(await self.pause_snapshot(),before)

    async def test_retirement_mysql_unenforced_check_is_refused(self):
        await db.init_db()
        async with self.database.connect() as conn:
            await conn.execute(text('ALTER TABLE node_retirements ALTER CHECK chk_node_retirement_revision NOT ENFORCED'));await conn.commit()
        with self.assertRaisesRegex(RuntimeError,'Retirement journal checks must be enforced'):await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT ENFORCED FROM information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='node_retirements' AND CONSTRAINT_NAME='chk_node_retirement_revision'")),'NO')

    async def test_retirement_mysql_foreign_trigger_is_refused_and_preserved(self):
        await db.init_db()
        async with self.database.connect() as conn:
            await conn.execute(text('CREATE TRIGGER foreign_retirement_trigger BEFORE INSERT ON node_retirements FOR EACH ROW SET NEW.revision=NEW.revision'));await conn.commit()
        with self.assertRaisesRegex(RuntimeError,'Unknown retirement journal trigger'):await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='foreign_retirement_trigger'")),1)

    async def test_retirement_mysql_wrong_named_index_cannot_masquerade_as_repaired(self):
        from app.services import node_retirement_schema as retirement
        await db.init_db()
        async with self.database.connect() as conn:
            await conn.execute(text('DROP INDEX idx_node_retirement_member ON node_retirements'))
            await conn.execute(text('CREATE INDEX idx_node_retirement_member ON node_retirements(master_id,phase)'));await conn.commit()
        async with self.database.connect() as conn:
            with self.assertRaisesRegex(RuntimeError,'Retirement journal indexes invalid'):await retirement.create(conn)
        with self.assertRaisesRegex(RuntimeError,'Retirement journal indexes invalid'):await db.init_db()

    async def test_retirement_mysql_column_collation_cannot_override_the_binary_table_contract(self):
        await db.init_db()
        async with self.database.connect() as conn:
            await conn.execute(text('ALTER TABLE node_retirements MODIFY authorization_sha256 VARCHAR(64) COLLATE utf8mb4_0900_ai_ci NOT NULL'));await conn.commit()
        with self.assertRaisesRegex(RuntimeError,'Retirement journal columns require binary collation'):await db.init_db()


if __name__ == "__main__":
    unittest.main()
