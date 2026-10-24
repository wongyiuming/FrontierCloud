"""Whole-task cancellation reconciles actual usage and retains committed owners."""
import os
import unittest
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import func,select,update

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_catalog import MARKER
from app.services.resource_cancel_finalize_master import CancellationFinalization,KEY
from app.services.resource_cancel_source_release import SourceRelease
from app.services.resource_cancel_target_release import TargetRelease
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_catalog as fixtures


@unittest.skipUnless(os.name=='posix','whole cancellation requires native Linux files')
class MasterCancellationFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.CurrentCancellationCatalogTests.asyncSetUp(self)
        self.finalizer=self.new_finalizer();self.release_calls=[];self.discard=set();self.corrupt=None
        original=self.f.transport.request
        services={'source':SourceRelease(self.f.source.transfers),'target':TargetRelease(self.f.target.transfers)}
        async def request(origin,path,**kwargs):
            side='source' if path.endswith('/cancel-source-release') else 'target' if path.endswith('/cancel-target-release') else None
            if side is None:return await original(origin,path,**kwargs)
            peer=getattr(self.f,side)
            self.assertEqual((origin,kwargs['credential'],kwargs['relation']),(peer.store.node['endpoint'],peer.credential,peer.relationship))
            async with peer.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            value=await services[side].advance(peer.store.node,relation,**kwargs['value']);self.release_calls.append(side)
            if side in self.discard and value['phase']=='released':
                self.discard.remove(side);raise httpx.ReadTimeout('Fixture discarded '+side+' release')
            if self.corrupt:value=self.corrupt(side,value)
            return p.sign(peer.store.unseal(peer.store.node['private_key']),value)
        self.f.transport.request=request

    asyncTearDown=fixtures.CurrentCancellationCatalogTests.asyncTearDown
    new_catalog=fixtures.CurrentCancellationCatalogTests.new_catalog

    def new_finalizer(self):return CancellationFinalization(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def final_step(self):return await self.finalizer.advance(self.lease)

    async def reach(self,phase):
        for _ in range(100):
            result=await self.final_step()
            if result['phase']==phase:return result
        self.fail('Master cancellation did not reach '+phase)

    async def pending(self):
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.tasks.c.state)),'cancelling')
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.drains)),1)
        await self.case.retained()

    async def restart(self):
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.runners).values(expires_at=0))
        self.lease=await self.finalizer.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        self.finalizer=self.new_finalizer()

    async def test_mixed_completion_reconciles_only_source_usage_and_preserves_every_byte(self):
        async with self.f.master.database.connect() as conn:
            target_before=await conn.scalar(select(fs.storage_members.c.used_bytes).where(fs.storage_members.c.member_id==self.f.target.store.node['node_id']))
        result=await self.reach('cancelled');self.assertTrue(result['task_completed'])
        actual=sum(len(data) for _,_,data in self.case.resources[1:])
        async with self.f.master.database.connect() as conn:
            task=dict((await conn.execute(select(s.tasks))).mappings().one())
            self.assertEqual(task['committed_bytes'],len(self.case.resources[0][2]))
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes).where(fs.storage_members.c.member_id==task['source_id'])),actual)
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes).where(fs.storage_members.c.member_id==task['target_id'])),target_before)
            self.assertEqual(self.finalizer._recorded(task),result['completion'])
            for table in (s.scopes,s.capacity,s.drains):self.assertEqual(await conn.scalar(select(func.count()).select_from(table)),0)
        await self.case.retained()

    async def test_lost_peer_replies_new_runners_and_terminal_replay_never_reset_later_usage(self):
        self.discard={'source','target'};lost=0
        for _ in range(100):
            try:result=await self.final_step()
            except httpx.ReadTimeout:
                lost+=1;await self.restart();continue
            if result['task_completed']:break
        else:self.fail('Lost-reply reconciliation did not converge')
        self.assertEqual(lost,2)
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==self.f.source.store.node['node_id'])
                .values(used_bytes=fs.storage_members.c.used_bytes+1234))
        self.f.now+=100
        with patch.object(self.f.master.store,'log',side_effect=AssertionError('Replayed terminal mutation')):replay=await self.final_step()
        self.assertEqual(replay['completion'],result['completion']);await self.case.retained()
        async with self.f.master.database.connect() as conn:
            actual=sum(len(data) for _,_,data in self.case.resources[1:])+1234
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes).where(fs.storage_members.c.member_id==self.f.source.store.node['node_id'])),actual)

    async def test_final_audit_failure_rolls_back_master_drain_and_usage_then_recovers(self):
        await self.reach('history');original=self.f.master.store.log
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_cancelled':raise RuntimeError('Fixture final audit failure')
            return await original(conn,action,*args,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'final audit failure'):await self.final_step()
        await self.pending();await self.restart();self.assertTrue((await self.reach('cancelled'))['task_completed'])
        await self.case.retained()

    async def test_signed_peer_reply_with_boolean_epoch_refuses(self):
        await self.reach('source_released')
        self.corrupt=lambda side,value:{**value,'epoch':True} if side=='target' else value
        with self.assertRaisesRegex(TransferError,'peer_reply_invalid'):await self.final_step()
        await self.pending();self.corrupt=None;await self.reach('cancelled');await self.case.retained()

    async def test_expired_runner_during_final_audit_rolls_back_master_completion(self):
        await self.reach('history');original=self.f.master.store.log
        async def delay(conn,action,*args,**kwargs):
            result=await original(conn,action,*args,**kwargs)
            if action=='migration_cancelled':self.f.now+=61
            return result
        with patch.object(self.f.master.store,'log',side_effect=delay):
            with self.assertRaisesRegex(TransferError,'lease_expired'):await self.final_step()
        await self.pending()

    async def test_unsigned_history_cursor_change_cannot_skip_validation(self):
        await self.reach('history')
        async with self.f.master.database.begin() as conn:
            task=(await conn.execute(select(s.tasks))).mappings().one();plan=task['plan']
            await conn.execute(update(s.tasks).values(plan={**plan,KEY:{**plan[KEY],'after':'f'*64}}))
        with self.assertRaisesRegex(TransferError,'progress_changed'):await self.final_step()
        await self.pending()

    async def test_changed_frozen_master_catalog_refuses_after_native_source_release(self):
        await self.reach('source_released');before=len(self.release_calls)
        async with self.f.master.database.begin() as conn:
            task=(await conn.execute(select(s.tasks))).mappings().one();plan=task['plan']
            await conn.execute(update(s.tasks).values(plan={**plan,MARKER:{**plan[MARKER],'retained_bytes':0}}))
        with self.assertRaisesRegex(TransferError,'catalog_changed'):await self.final_step()
        self.assertEqual(len(self.release_calls),before);await self.pending()

    async def test_cleanup_grace_history_cannot_be_relabelled_to_skip_the_hour(self):
        await self.reach('target_released')
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(s.units).where(s.units.c.state=='cleaned').values(cleanup_after=s.units.c.committed_at+1))
        with self.assertRaisesRegex(TransferError,'cleanup_history_changed'):await self.final_step()
        await self.pending()


if __name__=='__main__':unittest.main()
