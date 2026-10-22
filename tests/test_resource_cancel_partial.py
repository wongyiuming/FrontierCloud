"""Genuine committed + partial + absent copies; cleanup grace is virtual."""
import os
import json
import traceback
import unittest
from unittest.mock import patch

from sqlalchemy import delete,select,update

from app.services import media_crypto,media_crypto_schema as cs,resource_migration_schema as s
from app.services.business_traffic import Monitor
from app.services import resource_migration_scheduler as scheduler_module
from app.services.federation import protocol as p,schema as fs
from app.services.node_retirement_history import NativeHistory
from app.services.node_retirement_master_history import MasterHistory
from app.services.node_retirement_fences import RetirementError
from app.services.resource_cancel_census import SourceCensus
from app.services.resource_cancel_private_receipts import FORMAT
from app.services.resource_cancel_source_release import SourceRelease
from app.services.resource_cancel_target_release import TargetRelease
from app.services.resource_cancel_target import selected,verify_receipt
from app.services.resource_migration_admission import Admission
from app.services.resource_observation import digest
from app.services.resource_transfer_files import DIRECTORY
from app.services.resource_transfer_protocol import WINDOW_BYTES
from app.services.resource_transfer_protocol import TransferError
from app.services.resource_transfer_peers import TransferPeers
from tests import test_resource_scheduler as fixtures,test_resource_cancel_master as controls


@unittest.skipUnless(os.name=='posix','mixed cancellation requires native Linux paths')
class PartialCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.x=fixtures.SchedulerTests('runTest');await self.x.asyncSetUp();self.f=self.x.f
        self.replacements=[]
        for peer in (self.f.source,self.f.target):
            peer.reservations.wall=lambda:self.f.now;peer.observer.wall=lambda:self.f.now
        self.calls=controls.native_transport(self.f,self.assertEqual)
        original=self.f.transport.request
        async def request(origin,path,**kwargs):
            action=path.rsplit('/',1)[-1]
            service={'cancel-source-census':SourceCensus,'cancel-source-release':SourceRelease,'cancel-target-release':TargetRelease}.get(action)
            if service is None:return await original(origin,path,**kwargs)
            peer=self.f.target if action=='cancel-target-release' else self.f.source
            self.assertEqual((origin,kwargs['credential'],kwargs['relation']),(peer.store.node['endpoint'],peer.credential,peer.relationship))
            async with peer.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            result=await service(peer.transfers).advance(peer.store.node,relation,**kwargs['value'])
            return p.sign(peer.store.unseal(peer.store.node['private_key']),result)
        self.f.transport.request=request

        original_code=scheduler_module.error_code
        def classified(error):
            code=original_code(error)
            if code=='migration_execution_internal_error':traceback.print_exception(error)
            return code
        self.diagnostics=patch.object(scheduler_module,'error_code',side_effect=classified);self.diagnostics.start()

    async def asyncTearDown(self):
        try:
            for service in self.replacements:await service.close()
        finally:
            try:await self.x.asyncTearDown()
            finally:self.diagnostics.stop()

    async def units(self):
        async with self.f.master.database.connect() as conn:
            return [dict(row) for row in (await conn.execute(select(s.units).order_by(s.units.c.unit_id))).mappings()]

    async def prepare(self,*,first_bytes=None,first_encrypted=False):
        source=self.f.source.store.node['node_id']
        parents=sorted((f'music/partial-{i:02}' for i in range(3)),key=lambda parent:digest(['media',source,parent]))
        self.first=await self.f.media(b'committed-original'*200 if first_bytes is None else first_bytes,
            path=parents[0]+'/first.mp3',encrypted=first_encrypted)
        self.done=await self.f.media(b'verified-private'*200,path=parents[1]+'/done.mp3')
        self.partial=await self.f.media(b'opaque-long-original'+b'abcd'*(2*WINDOW_BYTES//4),path=parents[1]+'/partial.mp3',encrypted=True)
        self.never=await self.f.media(b'never-started-original'*200,path=parents[1]+'/never.mp3',encrypted=True)
        self.absent=await self.f.media(b'untouched-parent'*200,path=parents[2]+'/absent.mp3')
        self.x.resources=[self.first,self.done,self.partial,self.never,self.absent]
        self.x.plan=await self.f.plan();self.assertEqual(self.x.plan['state'],'ready',self.x.plan)
        await Admission(self.f.master.store,self.f.transport,preview_epoch=self.f.planner.epoch,
            traffic=self.f.traffic,wall=lambda:self.f.now).prepare(self.x.plan['plan_id'],self.x.plan['review_sha256'],'admin')
        for _ in range(120):
            await self.x.scheduler.tick();task=await self.x.task();units=await self.units()
            self.assertNotEqual(task['state'],'failed',task['error_code'])
            if units[0]['state']=='committed' and units[0]['native_acknowledged']==1:break
        else:self.fail('first parent did not commit')
        self.assertEqual(units[1]['state'],'pending');self.assertEqual(units[2]['state'],'pending')
        await self.x.scheduler.tick();self.assertEqual((await self.x.task())['state'],'copying')
        transfer=self.x.scheduler.transfer
        self.x.scheduler.lease,result=await transfer.copy_one(self.x.scheduler.lease,self.done[0]);self.assertEqual(result['state'],'verified')
        self.x.scheduler.lease,result=await transfer.copy_one(self.x.scheduler.lease,self.partial[0])
        self.assertEqual((result['state'],result['received_bytes']),('receiving',WINDOW_BYTES))
        self.partial_size=result['received_bytes'];self.unit_id=units[1]['unit_id'];self.absent_unit=units[2]['unit_id']
        self.part=self.f.target.roots['media']/DIRECTORY/task['task_id']/self.unit_id/(self.partial[0]+'.part')
        self.assertEqual(self.part.read_bytes(),self.partial[2][:WINDOW_BYTES])
        task=await self.x.task();await self.x.scheduler.cancellation.request(task['task_id'],task['revision'],'admin')
        self.assertEqual((await self.x.task())['state'],'cancelling')

    async def settled(self):
        for _ in range(320):
            await self.x.scheduler.tick();task=await self.x.task();units=await self.units()
            self.assertNotEqual(task['state'],'failed',task['error_code'])
            if all(unit['state']=='cancelled' and unit['native_acknowledged']==1 for unit in units[1:]):return units
        self.fail('partial and absent parents did not settle')

    async def finish(self):
        await self.settled()
        for _,path,data in self.x.resources:self.assertEqual(path.read_bytes(),data)
        await self.x.advance_clock(3601)
        return await self.x.reach('cancelled',limit=320)

    async def histories(self):
        for peer in (self.f.master,self.f.source,self.f.target):
            async with peer.database.connect() as conn:
                before={table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in s.metadata.tables.values()}
                with patch.object(p,'sign',side_effect=AssertionError('History issued authority')):
                    value=(await MasterHistory(peer.store,self.f.now).verify(conn,peer.store.node,self.f.source.store.node['node_id']) if peer is self.f.master else
                        await NativeHistory(peer.store,self.f.now).verify(conn,peer.store.node))
                self.assertEqual(value,{'master_tasks' if peer is self.f.master else 'native_tasks':1})
                after={table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in s.metadata.tables.values()}
                self.assertEqual(before,after)

    async def fail_after_unlink(self):
        # Claim the cancellation runner after invalidating the old copy lease.
        for _ in range(4):
            await self.x.scheduler.tick()
            if self.x.scheduler.lease is not None:break
        self.assertIsNotNone(self.x.scheduler.lease)
        original=self.f.target.store.log
        async def interrupted(conn,action,*args,**values):
            if action=='migration_private_copy_cancelled' and values.get('logical_id')==self.partial[0]:
                raise RuntimeError('Fixture audit interruption after private unlink')
            return await original(conn,action,*args,**values)
        with patch.object(self.f.target.store,'log',side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError,'audit interruption after private unlink'):
                for _ in range(180):
                    self.x.scheduler.lease,_=await self.x.scheduler.cancel_execution.advance(self.x.scheduler.lease)
                self.fail('partial unlink fault was not reached')
        self.assertFalse(self.part.exists())
        async with self.f.target.database.connect() as conn:
            entry=dict((await conn.execute(select(s.storage_journal).where(s.storage_journal.c.logical_id==self.partial[0]))).mappings().one())
        self.assertEqual(entry['state'],'aborting');self.assertEqual(entry['sha256'],'')
        return entry['cleanup_version']

    async def restart(self):
        old=self.f.target.transfers;await old.close()
        replacement=TransferPeers(self.f.target.store,root_provider=lambda:self.f.target.roots,traffic=self.f.traffic,
            wall=lambda:self.f.now,client=old.client)
        self.f.target.transfers=replacement
        self.replacements.append(replacement)
        await self.x.scheduler.stop();self.x.scheduler=self.x.new_scheduler()

    async def test_actual_partial_and_absent_copies_finish_without_fabricating_verification_or_deletion(self):
        await self.prepare()
        with patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('Media decrypted')):
            row=await self.finish()
        self.assertEqual(row['committed_bytes'],len(self.first[2]));self.assertFalse(self.first[1].exists())
        self.assertFalse(self.part.exists())
        for logical,path,data in self.x.resources[1:]:self.assertEqual(path.read_bytes(),data)
        async with self.f.target.database.connect() as conn:
            units={r['unit_id']:dict(r) for r in (await conn.execute(select(s.units))).mappings()}
            receipt=units[self.unit_id]['publication']['target_cancel']['receipt']['payload']
            self.assertEqual(receipt['format'],FORMAT)
            self.assertEqual((receipt['processed_count'],receipt['deleted_count'],receipt['absent_count']),(3,2,1))
            self.assertEqual(receipt['deleted_bytes'],len(self.done[2])+self.partial_size)
            self.assertLess(receipt['deleted_bytes'],receipt['total_bytes'])
            absent=units[self.absent_unit]['publication']['target_cancel']['receipt']['payload']
            self.assertEqual((absent['processed_count'],absent['deleted_count'],absent['deleted_bytes'],absent['absent_count']),(1,0,0,1))
            envelope=units[self.unit_id]['publication']['target_cancel']['receipt']
        grant=row['plan']['cancel_completion']['payload']['grant']['payload'];unit=selected(units[self.unit_id],grant)
        master_key=p.public_key(self.f.master.store.unseal(self.f.master.store.node['private_key']))
        for changes in ({'processed_count':2},{'processed_bytes':0},{'deleted_count':True},{'deleted_bytes':-1},
                {'absent_count':0},{'format':'frontiercloud-python-target-shadow-cancelled'}):
            changed=p.sign(self.f.target.store.unseal(self.f.target.store.node['private_key']),{**envelope['payload'],**changes})
            with self.assertRaises(TransferError):verify_receipt(changed,grant['target_key'],master_key,grant,unit,self.f.now)
        async with self.f.master.database.connect() as conn:
            objects={r['logical_id']:dict(r) for r in (await conn.execute(select(s.objects))).mappings()}
            for resource in (self.partial,self.never,self.absent):
                self.assertEqual((objects[resource[0]]['state'],objects[resource[0]]['verified_bytes'],objects[resource[0]]['target_sha256']),('cancelled',0,''))
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes).where(fs.storage_members.c.member_id==self.f.target.store.node['node_id'])),len(self.first[2]))
        await self.histories()

    async def test_master_only_descriptors_preserve_retained_source_census_and_readonly_history(self):
        media=self.f.media
        async def uploaded(*args,**kwargs):
            resource=await media(*args,**kwargs)
            if kwargs.get('encrypted'):
                async with self.f.source.database.begin() as conn:await conn.execute(delete(cs.encryption))
            return resource
        with patch.object(self.f,'media',side_effect=uploaded):await self.prepare(first_encrypted=True)
        with patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted')):
            row=await self.finish()
        self.assertEqual(row['committed_bytes'],len(self.first[2]))
        for _,path,data in self.x.resources[1:]:self.assertEqual(path.read_bytes(),data)
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.peer_reservations.c.state)),'released')
            self.assertEqual(await conn.scalar(select(cs.encryption.c.file_id)),None)
        await self.histories()
        async with self.f.master.database.connect() as conn:
            encoded=await conn.scalar(select(s.plan_bindings.c.object_json).where(s.plan_bindings.c.logical_id==self.partial[0]))
        original=json.loads(encoded);missing={k:v for k,v in original.items() if k!='source_encryption_sha256'}
        for damaged in ({**original,'source_encryption_sha256':'b'*64},missing):
            async with self.f.master.database.begin() as conn:
                await conn.execute(update(s.plan_bindings).where(s.plan_bindings.c.logical_id==self.partial[0])
                    .values(object_json=p.canonical(damaged).decode()))
            async with self.f.master.database.connect() as conn:
                with self.assertRaises(RetirementError):
                    await MasterHistory(self.f.master.store,self.f.now).verify(conn,self.f.master.store.node,self.f.source.store.node['node_id'])
                self.assertEqual(await conn.scalar(select(s.plan_bindings.c.object_json).where(s.plan_bindings.c.logical_id==self.partial[0])),
                    p.canonical(damaged).decode())

    async def test_target_restart_after_unlink_and_lost_sql_ack_reuses_the_persisted_intent(self):
        await self.prepare();intent=await self.fail_after_unlink();await self.restart();await self.finish()
        async with self.f.target.database.connect() as conn:
            entry=dict((await conn.execute(select(s.storage_journal).where(s.storage_journal.c.logical_id==self.partial[0]))).mappings().one())
        self.assertEqual((entry['state'],entry['cleanup_version']),('aborted',intent))
        for _,path,data in self.x.resources[1:]:self.assertEqual(path.read_bytes(),data)
        await self.histories()

    async def test_reappeared_file_after_unlink_is_retained_with_scopes_and_reservations(self):
        await self.prepare();await self.fail_after_unlink();self.part.write_bytes(b'foreign reappeared bytes');self.part.chmod(0o600)
        with self.assertRaisesRegex(TransferError,'stage_changed'):
            for _ in range(40):self.x.scheduler.lease,_=await self.x.scheduler.cancel_execution.advance(self.x.scheduler.lease)
        self.assertEqual(self.part.read_bytes(),b'foreign reappeared bytes')
        for _,path,data in self.x.resources:self.assertEqual(path.read_bytes(),data)
        for peer in (self.f.master,self.f.target):
            async with peer.database.connect() as conn:
                self.assertGreater(await conn.scalar(select(s.capacity.c.reserved_bytes)),0)

    async def test_unjournalled_empty_file_is_not_adopted_as_an_absent_copy(self):
        await self.prepare()
        unknown=self.part.with_name(self.never[0]+'.part');unknown.write_bytes(b'');unknown.chmod(0o600)
        for _ in range(4):
            await self.x.scheduler.tick()
            if self.x.scheduler.lease is not None:break
        with self.assertRaisesRegex(TransferError,'unjournalled_file'):
            for _ in range(180):self.x.scheduler.lease,_=await self.x.scheduler.cancel_execution.advance(self.x.scheduler.lease)
        self.assertTrue(unknown.is_file());self.assertEqual(unknown.stat().st_size,0)
        for _,path,data in self.x.resources:self.assertEqual(path.read_bytes(),data)
        for peer in (self.f.master,self.f.target):
            async with peer.database.connect() as conn:
                self.assertGreater(await conn.scalar(select(s.capacity.c.reserved_bytes)),0)

    async def test_due_committed_cleanup_waits_for_source_reader_and_finishes_after_close(self):
        await self.prepare();await self.settled()
        observed=Monitor(monotonic=lambda:self.f.now,wall=lambda:self.f.now);observed.snapshot()
        self.f.source.transfers.traffic=observed
        await self.x.advance_clock(3601);observed.entered()
        try:
            for _ in range(200):
                await self.x.scheduler.tick();task=await self.x.task()
                self.assertNotEqual(task['state'],'failed',task['error_code'])
                if task['error_code']:
                    self.assertEqual(task['error_code'],'migration_waiting_reader');break
            else:self.fail('Due cleanup did not expose the active Source reader')
            self.assertTrue(self.first[1].is_file())
            previous_epoch=(await self.units())[0]['publication']['target_live_reply']['proof']['payload']['epoch']
        finally:observed.exited()
        await self.x.advance_clock(31);await self.x.reach('cancelled',limit=320)
        self.assertGreater((await self.units())[0]['publication']['target_live_reply']['proof']['payload']['epoch'],previous_epoch)
        self.assertFalse(self.first[1].exists());await self.histories()
