"""Readonly retirement checks over histories produced by actual migration paths."""
import copy
import os
import unittest
from unittest.mock import patch

from sqlalchemy import delete,insert,select,update

from app.services import resource_migration_schema as s
from app.services import media_crypto
from app.services.federation import protocol as p
from app.services.federation import schema as fs
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_history import NativeHistory
from app.services.node_retirement_master_history import MasterHistory
from app.services.resource_observation import digest
from tests import test_resource_finalization as completed,test_resource_transfer_cancel as cancelled,test_resource_peer_reservations as unstarted,test_resource_migration_admission as admission


@unittest.skipUnless(os.name=='posix','migration history fixtures use native Linux files')
class HistoryFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.fixture=None

    async def asyncTearDown(self):
        if self.fixture:await self.fixture.asyncTearDown()

    async def fixture_for(self,cls):
        self.fixture=cls('runTest');await self.fixture.asyncSetUp();self.f=self.fixture.f
        # Existing fixtures mix real observations with a virtual cleanup clock.
        # Use one clock for all records in these history-specific cases.
        for peer in (self.f.source,self.f.target):
            peer.reservations.wall=lambda:self.f.now
            peer.observer.wall=lambda:self.f.now
        return self.fixture

    async def verify(self,peer,member_id=None):
        async with peer.database.connect() as conn:
            before={table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in s.metadata.tables.values()}
            try:
                with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted media')),
                        patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster')),
                        patch.object(p,'sign',side_effect=AssertionError('issued capability')),
                        patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read payload'))):
                    if member_id is not None:return await MasterHistory(peer.store,self.f.now).verify(conn,peer.store.node,member_id)
                    return await NativeHistory(peer.store,self.f.now).verify(conn,peer.store.node)
            finally:
                after={table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in s.metadata.tables.values()}
                self.assertEqual(before,after,'history verification must never change a row')

    async def complete(self):
        fixture=await self.fixture_for(completed.FinalizationTests);await fixture.prepare();await fixture.finish()
        return fixture


class NativeHistoryTests(HistoryFixture):
    async def test_actual_completed_source_and_target_history_is_readonly(self):
        await self.complete()
        for peer in (self.f.source,self.f.target):self.assertEqual(await self.verify(peer),dict(native_tasks=1))

    async def test_partial_and_verified_cancellation_keep_valid_history(self):
        for partial in (True,False):
            fixture=await self.fixture_for(cancelled.TransferCancellationTests)
            await fixture.prepare(partial=partial);await fixture.finish()
            for peer in (self.f.source,self.f.target):self.assertEqual(await self.verify(peer),dict(native_tasks=1))
            await fixture.asyncTearDown();self.fixture=None

    async def test_unstarted_and_absence_cancellation_are_valid_without_units(self):
        fixture=await self.fixture_for(unstarted.PeerReservationTests)
        await fixture.call('cancel_unstarted',reservation=fixture.request)
        self.assertEqual(await self.verify(fixture.peer),dict(native_tasks=1))
        fixture.request=fixture.reservation();await fixture.call('begin',reservation=fixture.request)
        await fixture.call('add_unit',task_id=fixture.request['task_id'],unit=fixture.units[0])
        await fixture.call('cancel_unstarted',reservation=fixture.request)
        self.assertEqual(await self.verify(fixture.peer),dict(native_tasks=2))

    async def test_live_or_forged_history_refuses_without_mutation(self):
        fixture=await self.fixture_for(cancelled.TransferCancellationTests);await fixture.prepare()
        with self.assertRaises(RetirementError):await self.verify(self.f.target)
        await fixture.finish();peer=self.f.target
        async with peer.database.begin() as conn:
            raw=dict((await conn.execute(select(s.peer_reservations))).mappings().one())
            payload=copy.deepcopy(raw['payload']);payload['owner_binding']='0'*64
            await conn.execute(update(s.peer_reservations).values(payload=payload))
        with self.assertRaisesRegex(RetirementError,'owner_changed'):await self.verify(peer)

    async def test_completed_history_requires_signed_receipt_and_exact_journal(self):
        await self.complete();peer=self.f.source
        async with peer.database.connect() as conn:raw=await conn.scalar(select(s.peer_reservations.c.payload))
        for name in ('signature','payload'):
            changed=copy.deepcopy(raw)
            if name=='signature':changed['final_receipt']['signature']='A'*86
            else:changed['final_receipt']['payload']['total_bytes']+=1
            async with peer.database.begin() as conn:await conn.execute(update(s.peer_reservations).values(payload=changed))
            with self.assertRaisesRegex(RetirementError,'receipt_invalid'):await self.verify(peer)
        async with peer.database.begin() as conn:
            await conn.execute(update(s.peer_reservations).values(payload=raw))
            await conn.execute(update(s.storage_journal).values(cleanup_version='0'*64))
        with self.assertRaises(RetirementError):await self.verify(peer)

    async def test_terminal_history_cannot_retain_task_reservations(self):
        fixture=await self.fixture_for(unstarted.PeerReservationTests)
        await fixture.call('cancel_unstarted',reservation=fixture.request);peer=fixture.peer;task=fixture.request['task_id']
        rows=((s.capacity,dict(task_id=task,member_id=peer.store.node['node_id'],reserved_bytes=0,updated_at=int(self.f.now))),
            (s.scopes,dict(scope_id='1'*64,task_id=task,member_id=peer.store.node['node_id'],resource_kind='media',parent_path='music/test',created_at=int(self.f.now))),
            (s.drains,dict(owner_id=task,member_id=peer.store.node['node_id'],reason='migration',created_at=int(self.f.now))))
        for table,row in rows:
            async with peer.database.begin() as conn:await conn.execute(insert(table).values(**row))
            with self.assertRaisesRegex(RetirementError,'work_remaining'):await self.verify(peer)
            async with peer.database.begin() as conn:await conn.execute(delete(table))

    async def test_changed_binding_or_oversized_metadata_is_preserved_and_refused(self):
        fixture=await self.fixture_for(cancelled.TransferCancellationTests);await fixture.prepare();await fixture.finish();peer=self.f.target
        async with peer.database.connect() as conn:row=dict((await conn.execute(select(s.peer_bindings))).mappings().one())
        async with peer.database.begin() as conn:await conn.execute(update(s.peer_bindings).values(binding_json=' '*17000))
        with self.assertRaisesRegex(RetirementError,'metadata_limit'):await self.verify(peer)
        async with peer.database.begin() as conn:await conn.execute(update(s.peer_bindings).values(binding_json=row['binding_json'],binding_sha256=digest(['forged'])))
        with self.assertRaises(RetirementError):await self.verify(peer)
        if peer.database.dialect.name=='sqlite':
            # SQLite does not enforce VARCHAR widths. A corrupted fixed-width
            # column must still be bounded before it reaches Python.
            async with peer.database.begin() as conn:
                await conn.execute(update(s.peer_bindings).values(binding_sha256=row['binding_sha256'],source_path='x'*100000))
            with self.assertRaises(RetirementError):await self.verify(peer)

    async def test_orphan_native_children_are_not_treated_as_a_preview_cache(self):
        fixture=await self.fixture_for(cancelled.TransferCancellationTests);await fixture.prepare();await fixture.finish();peer=self.f.target
        async with peer.database.begin() as conn:await conn.execute(delete(s.peer_reservations))
        with self.assertRaisesRegex(RetirementError,'orphan'):await self.verify(peer)

    async def test_completed_owner_mode_change_does_not_rewrite_history(self):
        await self.complete();peer=self.f.source
        async with peer.database.begin() as conn:
            await conn.execute(update(fs.relationships).values(mode='Relay'))
        self.assertEqual(await self.verify(peer),dict(native_tasks=1))

    async def test_recording_history_preserves_opaque_footers_and_business_rows(self):
        fixture=await self.fixture_for(completed.FinalizationTests)
        cleanup=fixture.fixture;cutover=cleanup.fixture.fixture
        await cutover.test_recording_parent_rebind_preserves_opaque_footer_and_current_master_business()
        cleanup.resources=cutover.resources;cleanup.kind='recording';cleanup.unit=cutover.unit_id;cleanup.lease=cutover.lease
        await cleanup.advance_clock(3601);await cleanup.finish()
        fixture.lease=cleanup.lease;fixture.resources=cleanup.resources;await fixture.finish()
        for peer in (self.f.source,self.f.target):self.assertEqual(await self.verify(peer),dict(native_tasks=1))
        self.assertEqual(await self.verify(self.f.master,self.f.source.store.node['node_id']),dict(master_tasks=1))


class MasterHistoryTests(HistoryFixture):
    async def test_master_completed_plan_catalog_units_and_signatures(self):
        await self.complete()
        for peer in (self.f.source,self.f.target):
            self.assertEqual(await self.verify(self.f.master,peer.store.node['node_id']),dict(master_tasks=1))

    async def test_master_active_task_blocks_only_involved_member(self):
        fixture=await self.fixture_for(admission.MigrationAdmissionTests);await fixture.preview();await fixture.prepare()
        with self.assertRaises(RetirementError):await self.verify(self.f.master,self.f.source.store.node['node_id'])
        self.assertEqual(await self.verify(self.f.master,'0'*32),dict(master_tasks=0))

    async def test_master_unstarted_cancel_validates_immutable_preview_without_units(self):
        fixture=await self.fixture_for(admission.MigrationAdmissionTests);await fixture.preview(many=True);await fixture.prepare()
        await fixture.admission.cancel_unstarted(fixture.plan['plan_id'],'history-test')
        self.assertEqual(await self.verify(self.f.master,self.f.source.store.node['node_id']),dict(master_tasks=1))

    async def test_master_partial_and_verified_cancel_keep_valid_history(self):
        for partial in (True,False):
            fixture=await self.fixture_for(cancelled.TransferCancellationTests);await fixture.prepare(partial=partial);await fixture.finish()
            self.assertEqual(await self.verify(self.f.master,self.f.source.store.node['node_id']),dict(master_tasks=1))
            await fixture.asyncTearDown();self.fixture=None

    async def test_master_tampered_snapshot_unit_or_signed_completion_refuses(self):
        await self.complete();peer=self.f.master;member=self.f.source.store.node['node_id']
        async with peer.database.connect() as conn:
            raw=dict((await conn.execute(select(s.tasks))).mappings().one())
            binding=dict((await conn.execute(select(s.plan_bindings).limit(1))).mappings().one())
            unit=dict((await conn.execute(select(s.units))).mappings().one())
        async with peer.database.begin() as conn:await conn.execute(update(s.plan_bindings).where(s.plan_bindings.c.logical_id==binding['logical_id']).values(object_sha256='0'*64))
        with self.assertRaises(RetirementError):await self.verify(peer,member)
        async with peer.database.begin() as conn:
            await conn.execute(update(s.plan_bindings).where(s.plan_bindings.c.logical_id==binding['logical_id']).values(object_sha256=binding['object_sha256']))
            await conn.execute(update(s.units).values(source_version='0'*64))
        with self.assertRaises(RetirementError):await self.verify(peer,member)
        async with peer.database.begin() as conn:
            await conn.execute(update(s.units).values(source_version=unit['source_version']))
            changed=copy.deepcopy(raw['plan']);changed['completion']['receipts']['target']['payload']['native_used_after']+=1
            changed['completion_sha256']=digest(changed['completion'])
            await conn.execute(update(s.tasks).values(plan=changed))
        with self.assertRaisesRegex(RetirementError,'receipt_invalid'):await self.verify(peer,member)
