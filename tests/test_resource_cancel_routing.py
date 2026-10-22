"""Publication gates, durable cancel routing and all-committed terminal reuse."""
import os
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import select,update

from app.services import resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.node_retirement_master_history import MasterHistory
from app.services.resource_cancel_intent import KEY,published
from app.services.resource_final_receipts import CANCELLED_COMPLETION_FORMAT
from app.services.resource_finalize_master import Finalization
from app.services.resource_migration_journal import Conflict
from app.services.resource_publish_master import Publication
from app.services.resource_publish_peers import PublishPeers
from app.services.resource_transfer_cancel import Cancellation
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_finalization as completed,test_resource_transfer as copying


@unittest.skipUnless(os.name=='posix','cancel routing uses native Linux staging')
class CancellationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case=copying.ResourceTransferTests('runTest');await self.case.asyncSetUp();self.f=self.case.f
        self.cancel=Cancellation(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.pub=Publication(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def asyncTearDown(self):await self.case.asyncTearDown()

    async def prepare(self,*,partial=False):
        await self.case.prepare(large=partial);await self.case.queue();await self.case.step();self.lease=self.case.lease

    async def task(self):
        async with self.f.master.database.connect() as conn:return dict((await conn.execute(select(s.tasks))).mappings().one())

    async def committing(self):
        await self.prepare();self.lease=await self.pub.enter(self.lease);self.lease=await self.pub.enter(self.lease)

    async def test_partial_copy_cannot_cross_master_journal_or_native_publication_gates(self):
        await self.prepare(partial=True);path=self.case.stage();before=path.read_bytes()
        with self.assertRaisesRegex(TransferError,'copies_incomplete'):await self.pub.enter(self.lease)
        self.lease=await self.pub.transfer.journal.checkpoint(self.lease,'verifying',committed_bytes=0)
        with self.assertRaises(Conflict):await self.pub.transfer.journal.checkpoint(self.lease,'committing',committed_bytes=0)
        task=await self.task();relations=await self.pub.transfer.relations(task)
        grant=self.pub.transfer._grant(task,relations,self.lease.epoch,self.lease.runner_id,'publish')
        async with self.f.target.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
        # A task-wide flag is no longer a copy-completion barrier. The native
        # publisher must prove the selected parent before starting assembly.
        with self.assertRaisesRegex(TransferError,'metadata_incomplete'):
            await PublishPeers(self.f.target.transfers).advance(self.f.target.store.node,relation,
                uuid.uuid4().hex,grant,self.case.grants[-1]['payload']['object']['unit_id'])
        self.assertEqual(path.read_bytes(),before);self.assertEqual(self.case.original.read_bytes(),self.case.data)

    async def test_partial_copy_cancel_records_prepublication_without_native_calls(self):
        await self.prepare(partial=True)
        with patch.object(self.f.transport,'request',side_effect=AssertionError('Cancel request contacted a peer')):
            await self.cancel.request(self.lease.task_id,self.lease.revision,'admin')
        task=await self.task();self.assertFalse(published(task));self.assertEqual(task['plan'][KEY]['from_state'],'copying')
        self.assertEqual(self.case.original.read_bytes(),self.case.data)

    async def test_committing_route_is_durable_before_the_first_native_publish_reply(self):
        await self.committing()
        async with self.f.target.database.connect() as conn:
            header=(await conn.execute(select(s.peer_reservations))).mappings().one()
            self.assertIsNot(header['payload']['transfer'].get('publication_started'),True)
        await self.cancel.request(self.lease.task_id,self.lease.revision,'admin')
        task=await self.task();self.assertTrue(published(task));self.assertEqual(task['plan'][KEY]['from_state'],'committing')
        self.assertEqual(task['state'],'cancelling');self.assertEqual(self.case.original.read_bytes(),self.case.data)

    async def test_corrupt_published_phase_with_partial_roster_refuses_without_repairing_rows(self):
        await self.prepare(partial=True)
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.tasks).values(state='committing'))
        before=await self.task()
        with self.assertRaises(Conflict):await self.cancel.request(self.lease.task_id,self.lease.revision,'admin')
        self.assertEqual(await self.task(),before);self.assertEqual(self.case.original.read_bytes(),self.case.data)


@unittest.skipUnless(os.name=='posix','terminal reuse needs native Linux peers')
class CommittedCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case=completed.FinalizationTests('runTest');await self.case.asyncSetUp();self.f=self.case.f
        for peer in (self.f.source,self.f.target):
            peer.reservations.wall=lambda:self.f.now;peer.observer.wall=lambda:self.f.now
        self.cancel=Cancellation(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.service=Finalization(self.f.master.store,self.f.transport,outcome='cancelled',traffic=self.f.traffic,wall=lambda:self.f.now)

    async def asyncTearDown(self):await self.case.asyncTearDown()

    async def request(self):
        await self.cancel.request(self.case.lease.task_id,self.case.lease.revision,'admin')
        self.lease=await self.service.t.journal.claim(self.case.lease.task_id,uuid.uuid4().hex,ttl=60)

    async def reach(self,phase):
        for _ in range(40):
            result=await self.service.advance(self.lease)
            if result['phase']==phase:return result
        self.fail('Committed cancellation did not reach '+phase)

    async def test_source_already_released_reuses_its_success_receipt_without_cancel_stop(self):
        await self.case.prepare();await self.case.source_released()
        async with self.f.source.database.connect() as conn:original=(await conn.scalar(select(s.peer_reservations.c.payload)))['final_receipt']
        await self.request()
        result=await self.reach('cancelled');self.assertEqual(result['completion']['format'],CANCELLED_COMPLETION_FORMAT)
        self.assertEqual(result['completion']['receipts']['source'],original)
        async with self.f.master.database.connect() as conn:
            task=(await conn.execute(select(s.tasks))).mappings().one();self.assertEqual(task['committed_bytes'],task['total_bytes'])
            self.assertEqual(await MasterHistory(self.f.master.store,self.f.now).verify(conn,self.f.master.store.node,self.f.source.store.node['node_id']),dict(master_tasks=1))
        for peer in (self.f.source,self.f.target):
            async with peer.database.connect() as conn:
                header=(await conn.execute(select(s.peer_reservations))).mappings().one()
                self.assertEqual(header['state'],'released');self.assertIsNot(header['payload']['transfer'].get('cancel_started'),True)
        await self.case.fixture.target_preserved()

    async def test_failed_master_audit_after_both_native_releases_recovers_with_a_new_epoch(self):
        await self.case.prepare();await self.request();await self.reach('target_released');original=self.f.master.store.log
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_cancelled_all_committed':raise RuntimeError('Fixture final cancellation audit failure')
            return await original(conn,action,*args,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'audit failure'):await self.service.advance(self.lease)
        await self.service.t.journal.release(self.lease)
        self.lease=await self.service.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        self.assertTrue((await self.reach('cancelled'))['task_completed']);await self.case.fixture.target_preserved()

    async def test_terminal_duplicate_preserves_later_usage_without_audit_or_native_requests(self):
        await self.case.prepare();await self.request();first=await self.reach('cancelled')
        async with self.f.master.database.begin() as conn:await conn.execute(update(fs.storage_members).values(used_bytes=fs.storage_members.c.used_bytes+1234))
        async with self.f.master.database.connect() as conn:before=[dict(row) for row in (await conn.execute(select(fs.storage_members))).mappings()]
        with (patch.object(self.f.master.store,'log',side_effect=AssertionError('Repeated final audit')),
                patch.object(self.f.transport,'request',side_effect=AssertionError('Repeated native finalization'))):
            self.assertEqual((await self.service.advance(self.lease))['completion'],first['completion'])
        async with self.f.master.database.connect() as conn:self.assertEqual([dict(row) for row in (await conn.execute(select(fs.storage_members))).mappings()],before)
        await self.case.fixture.target_preserved()


if __name__=='__main__':unittest.main()
