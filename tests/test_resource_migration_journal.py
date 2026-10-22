"""Actual SQLite restart, schema, lease fencing and explicit pause controls."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import insert, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from app.core import db, schema_migrations
from app.core.sqlite_compat import create_sqlite_engine
from app.services import resource_migration_schema as s
from app.services.federation.state import State
from app.services.resource_migration_journal import Conflict, Journal
from app.services.business_traffic import Monitor

TASK, SOURCE, TARGET, RUNNER, OTHER = [char * 32 for char in 'abcde']
UNIT = 'a' * 64


class MigrationJournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = create_sqlite_engine(URL.create('sqlite+aiosqlite', database=str(self.root/'state.db')))
        self.patches = [patch.object(db, 'engine', self.database), patch.object(db, 'SECRET_DIR', self.root/'secrets')]
        for item in self.patches:
            item.start()
        await db.init_db()
        self.store = State(self.database, Fernet.generate_key())
        await self.store.initialize()
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE node_identity SET role='Master'"))
            await conn.execute(insert(s.tasks).values(task_id=TASK, master_id=self.store.node['node_id'],
                source_id=SOURCE, target_id=TARGET, source_relationship=SOURCE, target_relationship=TARGET,
                plan_sha256='f'*64, plan={}, state='queued', object_count=1, total_bytes=123,
                created_at=1, updated_at=1))
            await conn.execute(insert(s.units).values(task_id=TASK, unit_id=UNIT, resource_kind='media',
                parent_path='music/历史', source_version='f'*64, state='pending',
                object_count=1, total_bytes=123))
            await conn.execute(insert(s.capacity).values(task_id=TASK, member_id=TARGET,
                reserved_bytes=123, updated_at=1))
        self.now = 1000
        self.traffic = Monitor(monotonic=lambda: self.now, wall=lambda: self.now)
        self.journal = Journal(self.store, clock=lambda: self.now, traffic=self.traffic)

    async def asyncTearDown(self):
        await self.database.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def task(self):
        async with self.database.connect() as conn:
            return dict((await conn.execute(select(s.tasks))).mappings().one())

    async def test_parallel_claims_have_one_winner_and_no_split_brain(self):
        results = await asyncio.gather(self.journal.claim(TASK, RUNNER),
                                       self.journal.claim(TASK, OTHER), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, Conflict) for result in results), 1)
        self.assertEqual((await self.task())['revision'], 2)

    async def test_expired_worker_cannot_renew_or_checkpoint_after_restart_takeover(self):
        old = await self.journal.claim(TASK, RUNNER)
        self.now += 16
        fresh = await Journal(self.store, clock=lambda: self.now).claim(TASK, OTHER)
        self.assertEqual(fresh.epoch, old.epoch + 1)
        for operation in (self.journal.renew(old), self.journal.checkpoint(old, 'copying', committed_bytes=0)):
            with self.assertRaises(Conflict):
                await operation
        await self.journal.renew(fresh)
        updated = await self.journal.checkpoint(fresh, 'copying', committed_bytes=0)
        self.assertEqual(updated.revision, fresh.revision + 1)

    async def test_active_lease_cannot_be_reclaimed_even_by_same_runner(self):
        await self.journal.claim(TASK, RUNNER)
        with self.assertRaises(Conflict):
            await self.journal.claim(TASK, RUNNER)

    async def test_failed_execution_retains_capacity_and_requires_explicit_revision_checked_retry(self):
        lease=await self.journal.claim(TASK,RUNNER)
        lease=await self.journal.checkpoint(lease,'copying',committed_bytes=0)
        await self.journal.fail(lease,'migration_transfer_staging_identity_changed')
        task=await self.task()
        self.assertEqual((task['state'],task['plan']['execution_failure']['state']),('failed','copying'))
        with self.assertRaises(Conflict):await self.journal.claim(TASK,OTHER)
        with self.assertRaises(Conflict):await self.journal.control(TASK,lease.revision,'retry','admin')
        revision=await self.journal.control(TASK,task['revision'],'retry','admin')
        self.assertEqual(((await self.task())['state'],(await self.task())['error_code']),('copying',''))
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),123)
            self.assertEqual(await conn.scalar(select(s.units.c.state)),'pending')
        fresh=await self.journal.claim(TASK,OTHER)
        self.assertGreater(fresh.epoch,lease.epoch)
        self.assertGreater(fresh.revision,revision)

    async def test_failure_audit_rollback_does_not_invent_a_failed_task(self):
        from unittest.mock import AsyncMock,patch
        lease=await self.journal.claim(TASK,RUNNER)
        with patch.object(self.store,'log',new=AsyncMock(side_effect=RuntimeError('audit unavailable'))):
            with self.assertRaisesRegex(RuntimeError,'audit unavailable'):
                await self.journal.fail(lease,'migration_execution_internal_error')
        self.assertEqual(((await self.task())['state'],(await self.task())['revision']),('queued',lease.revision))
        await self.journal.renew(lease)

    async def test_old_runner_release_cannot_expire_restarted_runner_or_release_capacity(self):
        old=await self.journal.claim(TASK,RUNNER)
        self.now+=16
        fresh=await self.journal.claim(TASK,OTHER)
        await self.journal.release(old)
        await self.journal.renew(fresh)
        async with self.database.connect() as conn:
            self.assertGreater(await conn.scalar(select(s.runners.c.expires_at)),self.now)
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),123)

    async def test_cancel_after_publication_begins_does_not_strand_the_task_in_cancelling(self):
        async with self.database.begin() as conn:
            await conn.execute(update(s.tasks).values(state='failed',plan={'execution_failure':{'state':'committing'}}))
            await conn.execute(update(s.units).values(state='publishing'))
        with self.assertRaises(Conflict):await self.journal.control(TASK,1,'cancel','admin')
        self.assertEqual((await self.task())['state'],'failed')
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),123)

    async def test_manual_pause_is_durable_and_never_automatically_resumed(self):
        lease = await self.journal.claim(TASK, RUNNER)
        revision = await self.journal.control(TASK, lease.revision, 'pause', 'admin')
        with self.assertRaises(Conflict):
            await self.journal.checkpoint(lease, 'copying', committed_bytes=0)
        restarted = Journal(self.store, clock=lambda: self.now, traffic=self.traffic)
        self.now += 30
        with self.assertRaises(Conflict):
            await restarted.control(TASK, revision, 'traffic_resume', 'scheduler')
        row = await self.task()
        self.assertEqual((row['state'], row['pause_reason'], row['resume_state']), ('paused', 'manual', 'queued'))
        revision = await restarted.control(TASK, revision, 'resume', 'admin')
        fresh = await restarted.claim(TASK, OTHER)
        self.assertGreater(fresh.revision, revision)
        self.assertGreater(fresh.epoch, lease.epoch)

    async def test_traffic_pause_can_be_promoted_to_manual_pause(self):
        revision = await self.journal.control(TASK, 1, 'traffic_pause', 'scheduler')
        with self.assertRaises(Conflict):
            await self.journal.control(TASK, revision, 'traffic_resume', 'scheduler')
        revision = await self.journal.control(TASK, revision, 'pause', 'admin')
        self.now += 30
        with self.assertRaises(Conflict):
            await self.journal.control(TASK, revision, 'traffic_resume', 'scheduler')
        await self.journal.control(TASK, revision, 'resume', 'admin')

    async def test_traffic_resume_requires_current_revision_and_quiet_proof(self):
        revision = await self.journal.control(TASK, 1, 'traffic_pause', 'scheduler')
        with self.assertRaises(Conflict):
            await self.journal.control(TASK, 1, 'traffic_resume', 'scheduler')
        self.now += 30
        await self.journal.control(TASK, revision, 'traffic_resume', 'scheduler')
        self.assertEqual((await self.task())['state'], 'queued')

    async def test_cancel_retains_capacity_until_verified_storage_abort(self):
        await self.journal.control(TASK, 1, 'cancel', 'admin')
        lease = await self.journal.claim(TASK, RUNNER)
        with self.assertRaises(Conflict):
            await self.journal.checkpoint(lease, 'cancelled', committed_bytes=0)
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)), 123)
        async with self.database.begin() as conn:
            await conn.execute(update(s.units).values(state='cancelled'))
        await self.journal.checkpoint(lease, 'cancelled', committed_bytes=0)

    async def test_progress_never_uses_copied_bytes_and_stages_cannot_be_skipped(self):
        lease = await self.journal.claim(TASK, RUNNER)
        with self.assertRaises(Conflict):
            await self.journal.checkpoint(lease, 'completed', committed_bytes=123)
        with self.assertRaises(Conflict):
            await self.journal.checkpoint(lease, 'copying', committed_bytes=123)
        lease = await self.journal.checkpoint(lease, 'copying', committed_bytes=0)
        lease = await self.journal.checkpoint(lease, 'verifying', committed_bytes=0)
        with self.assertRaises(Conflict):
            await self.journal.checkpoint(lease, 'committing', committed_bytes=0)

    async def test_each_pause_boundary_preserves_resources_and_invalidates_the_old_runner(self):
        for phase in ('queued','copying','verifying','committing','cleanup_pending'):
            with self.subTest(phase=phase):
                async with self.database.begin() as conn:
                    await conn.execute(update(s.tasks).values(state=phase,resume_state='',pause_reason=''))
                old=await self.journal.claim(TASK,RUNNER)
                revision=await self.journal.control(TASK,old.revision,'pause','admin')
                row=await self.task()
                self.assertEqual((row['state'],row['resume_state'],row['pause_reason']),('paused',phase,'manual'))
                with self.assertRaises(Conflict):await self.journal.renew(old)
                restarted=Journal(self.store,clock=lambda:self.now,traffic=self.traffic)
                self.now+=30
                with self.assertRaises(Conflict):await restarted.control(TASK,revision,'traffic_resume','scheduler')
                await restarted.control(TASK,revision,'resume','admin')
                self.assertEqual((await self.task())['state'],phase)
                async with self.database.connect() as conn:
                    self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),123)
                    self.assertEqual(await conn.scalar(select(s.units.c.state)),'pending')

    async def test_unproven_committing_phase_is_retained_for_reconciliation(self):
        async with self.database.begin() as conn:
            await conn.execute(update(s.tasks).values(state='committing'))
        with self.assertRaises(Conflict):await self.journal.control(TASK,1,'cancel','admin')
        self.assertEqual((await self.task())['state'],'committing')

    async def test_wrong_owner_or_nonmaster_cannot_take_a_job(self):
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE node_identity SET role='Follower'"))
        with self.assertRaises(ValueError):
            await self.journal.claim(TASK, RUNNER)
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE node_identity SET role='Master'"))
            await conn.execute(update(s.tasks).values(master_id=OTHER))
        with self.assertRaises(ValueError):
            await self.journal.claim(TASK, RUNNER)

    async def test_failed_audit_rolls_back_claim_and_runner(self):
        with patch.object(self.store, 'log', side_effect=RuntimeError('audit fault')):
            with self.assertRaisesRegex(RuntimeError, 'audit fault'):
                await self.journal.claim(TASK, RUNNER)
        self.assertEqual((await self.task())['revision'], 1)
        async with self.database.connect() as conn:
            self.assertIsNone(await conn.scalar(select(s.runners.c.epoch)))

    async def test_generation_four_upgrade_preserves_all_old_rows_and_rejects_missing_index(self):
        async with self.database.begin() as conn:
            for table in reversed(s.metadata.sorted_tables):
                await conn.run_sync(table.drop)
            await conn.execute(text('UPDATE frontiercloud_schema SET generation=4'))
            old_tables = await db._table_names(conn)
            before = {name: list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples())
                      for name in old_tables - {'frontiercloud_schema', 'frontiercloud_schema_migrations'}}
        await db.init_db()
        async with self.database.connect() as conn:
            for name, values in before.items():
                self.assertEqual(list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples()), values)
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')), schema_migrations.CURRENT_SCHEMA_GENERATION)
            self.assertEqual(await conn.scalar(text('SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=5')),
                             schema_migrations.MIGRATIONS[5].checksum)
        async with self.database.begin() as conn:
            await conn.execute(text('DROP INDEX idx_migration_task_state'))
        with self.assertRaisesRegex(RuntimeError, 'index missing'):
            await db.init_db()

    async def test_interrupted_sqlite_ddl_keeps_generation_and_retries_without_data_loss(self):
        async with self.database.begin() as conn:
            for table in reversed(s.metadata.sorted_tables):
                await conn.run_sync(table.drop)
            await conn.execute(text('UPDATE frontiercloud_schema SET generation=4'))
        original = s.validate
        with patch.object(s, 'validate', side_effect=RuntimeError('DDL interrupt')):
            with self.assertRaisesRegex(RuntimeError, 'generation marker 未推进'):
                await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')), 4)
            self.assertFalse(set(s.metadata.tables) & await db._table_names(conn))
        await db.init_db()
        async with self.database.connect() as conn:
            await original(conn)

    async def test_database_rejects_invalid_pause_state_and_negative_reservations(self):
        for statement in ("UPDATE migration_tasks SET state='paused'", 'UPDATE migration_capacity SET reserved_bytes=-1'):
            with self.assertRaises(IntegrityError):
                async with self.database.begin() as conn:
                    await conn.execute(text(statement))
