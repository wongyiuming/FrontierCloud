"""Actual three-store/native-file orchestration; cleanup time is virtual here."""
import asyncio
import json
import os
import unittest
import uuid
from unittest.mock import AsyncMock,patch

import httpx
from sqlalchemy import delete,func,insert,select,update

from app.services import media_crypto,media_crypto_schema as cs,resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.resource_migration_admission import Admission
from app.services.resource_migration_journal import Journal
from app.services.resource_migration_scheduler import Scheduler,error_code,TRANSIENT
from app.services.resource_transfer_protocol import TransferError
from app.services.resource_transfer_cancel import CancelPeers
from tests import test_resource_finalization as fixtures


class SchedulerErrorTests(unittest.TestCase):
    def test_tls_connection_and_timeout_are_distinct_without_echoing_remote_text(self):
        for error,expected in ((httpx.ConnectError('CERTIFICATE_VERIFY_FAILED private-value'),
                'migration_peer_tls_verification_failed'),(httpx.ConnectError('private-value'),
                'migration_peer_connect_failed'),(httpx.ReadTimeout('private-value'),'migration_peer_timeout')):
            with self.subTest(expected=expected):
                self.assertEqual(error_code(error),expected)
                self.assertNotIn('private-value',error_code(error))
        self.assertNotIn('migration_peer_tls_verification_failed',TRANSIENT)
        self.assertIn('migration_peer_connect_failed',TRANSIENT)
        self.assertEqual(error_code(TransferError('migration_cancel_target_release_busy')),'migration_waiting_writer')
        self.assertEqual(error_code(TransferError('migration_cancel_source_waiting_reader')),'migration_waiting_reader')


class CancellationWaitCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_base_and_peer_wait_states_share_the_exact_retryable_error_codes(self):
        from types import SimpleNamespace
        for prefix in ('','source_','target_'):
            for suffix in ('reader','writer','low_traffic'):
                phase=prefix+'waiting_'+suffix;expected='migration_waiting_'+suffix
                context=SimpleNamespace(lease=object(),cancel_execution=SimpleNamespace(advance=AsyncMock(return_value=(object(),{'phase':phase}))),
                    _delay=AsyncMock())
                await Scheduler._cancel(context,{})
                context._delay.assert_awaited_once_with(expected)
                self.assertIn(expected,TRANSIENT)


@unittest.skipUnless(os.name=='posix','scheduler acceptance uses native Linux peers')
class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.FinalizationTests('runTest');await self.fixture.asyncSetUp();self.f=self.fixture.f
        self.schedulers=[];self.clock=1000
        self.f.traffic._clock=lambda:self.clock
        self.f.traffic._wall=lambda:self.clock
        self.control_calls=[];self.request=self.f.transport.request;self.f.transport.request=self.control
        self.scheduler=self.new_scheduler()

    async def control(self,origin,path,**kwargs):
        self.control_calls.append(path)
        if path.rsplit('/',1)[-1] not in {'abort-stop','abort-clean'}:
            return await self.request(origin,path,**kwargs)
        from app.services.federation import protocol as p
        peer=next(peer for peer in (self.f.source,self.f.target) if peer.store.node['endpoint']==origin)
        async with peer.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships).where(
                fs.relationships.c.relationship_id==peer.relationship))).mappings().one())
        operation=CancelPeers(peer.transfers)
        result=await (operation.stop if path.endswith('abort-stop') else operation.clean)(
            peer.store.node,relation,**kwargs['value'])
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)

    async def asyncTearDown(self):
        for item in self.schedulers:await item.stop()
        await self.fixture.asyncTearDown()

    def new_scheduler(self,**options):
        value=Scheduler(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now,
            fenced=options.get('fenced',lambda:False))
        self.schedulers.append(value);return value

    async def task(self,task_id=None):
        async with self.f.master.database.connect() as conn:
            return dict((await conn.execute(select(s.tasks).where(s.tasks.c.task_id==(
                task_id or self.plan['plan_id'])))).mappings().one())

    async def reservations(self):
        async with self.f.master.database.connect() as conn:
            return [dict(row) for row in (await conn.execute(select(s.capacity))).mappings()]

    async def prepare(self):
        self.resources=[await self.f.media(b'ID3 ordinary original'*300),
            await self.f.media(b'private ciphertext original'*400,path='music/album/private.mp3',encrypted=True)]
        self.plan=await self.f.plan();self.assertEqual(self.plan['state'],'ready',self.plan)
        admission=Admission(self.f.master.store,self.f.transport,preview_epoch=self.f.planner.epoch,
            traffic=self.f.traffic,wall=lambda:self.f.now)
        await admission.prepare(self.plan['plan_id'],self.plan['review_sha256'],'admin')

    async def copying(self):
        await self.prepare()
        for _ in range(4):
            await self.scheduler.tick()
            if (await self.task())['state']=='copying':return
        self.fail('scheduler did not queue/copy prepared work')

    async def ready_to_copy(self):
        await self.copying()
        for _ in range(12):
            if (await self.task())['plan']['execution'].get('metadata',{}).get('complete'):return
            await self.scheduler.tick()
        self.fail('scheduler did not enroll complete object metadata')

    async def reach(self,state,limit=250):
        for _ in range(limit):
            await self.scheduler.tick();row=await self.task()
            if row['state']==state:return row
            if row['state']=='failed':self.fail(row['error_code'])
        self.fail('scheduler did not reach '+state)

    async def advance_clock(self,seconds):
        self.f.now+=seconds
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.relationships).values(last_heartbeat=int(self.f.now)))

    async def test_scheduler_drives_original_plain_and_ciphertext_through_native_completion(self):
        await self.prepare()
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster'))):
            row=await self.reach('cleanup_pending')
            self.assertEqual(row['committed_bytes'],sum(len(data) for _,_,data in self.resources))
            self.assertIsNone(self.scheduler.lease)
            for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)
            for _ in range(3):self.assertFalse(await self.scheduler.tick())
            await self.advance_clock(3601)
            completed=await self.reach('completed')
        self.assertEqual(set(completed['plan']['completion']['receipts']),{'source','target'})
        for logical,path,data in self.resources:
            self.assertFalse(path.exists())
            target=self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])
            self.assertEqual(target.read_bytes(),data)
        async with self.f.master.database.connect() as conn:
            self.assertEqual(set((await conn.execute(select(fs.global_media.c.storage_member_id))).scalars()),
                {self.f.target.store.node['node_id']})
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),0)

    async def test_master_only_descriptor_survives_publication_cleanup_and_no_source_backfill(self):
        for peer in (self.f.source,self.f.target):
            peer.reservations.wall=lambda:self.f.now;peer.observer.wall=lambda:self.f.now
        media=self.f.media
        async def uploaded(*args,**kwargs):
            resource=await media(*args,**kwargs)
            if kwargs.get('encrypted'):
                async with self.f.source.database.begin() as conn:await conn.execute(delete(cs.encryption))
            return resource
        with patch.object(self.f,'media',side_effect=uploaded):await self.prepare()
        async with self.f.master.database.connect() as conn:
            original=[dict(row) for row in (await conn.execute(select(cs.encryption))).mappings()]
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster'))):
            await self.reach('cleanup_pending')
            async with self.f.source.database.connect() as conn:
                self.assertEqual(await conn.scalar(select(func.count()).select_from(cs.encryption)),0)
            async with self.f.target.database.connect() as conn:
                self.assertEqual(await conn.scalar(select(func.count()).select_from(cs.encryption)),len(original))
            await self.advance_clock(3601);await self.reach('completed')
        async with self.f.master.database.connect() as conn:
            self.assertEqual([dict(row) for row in (await conn.execute(select(cs.encryption))).mappings()],original)
        for _,path,data in self.resources:
            self.assertFalse(path.exists())
            self.assertEqual((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).read_bytes(),data)
        from app.services.node_retirement_history import NativeHistory
        from app.services.node_retirement_master_history import MasterHistory
        from app.services.federation import protocol as p
        with patch.object(p,'sign',side_effect=AssertionError('History issued authority')):
            for peer in (self.f.master,self.f.source,self.f.target):
                async with peer.database.connect() as conn:
                    result=(await MasterHistory(peer.store,self.f.now).verify(conn,peer.store.node,self.f.source.store.node['node_id'])
                        if peer is self.f.master else await NativeHistory(peer.store,self.f.now).verify(conn,peer.store.node))
                    self.assertEqual(result,{'master_tasks' if peer is self.f.master else 'native_tasks':1})

    async def test_cancel_after_all_parents_commit_waits_for_grace_and_reuses_success_receipts(self):
        from app.services.resource_final_receipts import CANCELLED_COMPLETION_FORMAT
        await self.prepare();row=await self.reach('cleanup_pending')
        await self.scheduler.cancellation.request(row['task_id'],row['revision'],'admin')
        count=len(self.control_calls)
        for _ in range(3):self.assertFalse(await self.scheduler.tick())
        self.assertEqual(len(self.control_calls),count)
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)
        await self.advance_clock(3601)
        row=await self.reach('cancelled')
        self.assertEqual(row['plan']['completion']['format'],CANCELLED_COMPLETION_FORMAT)
        self.assertEqual(row['committed_bytes'],row['total_bytes'])
        self.assertFalse(any(path.endswith(('/cancel-stop','/cancel-source-stop')) for path in self.control_calls))
        for logical,path,data in self.resources:
            self.assertFalse(path.exists())
            self.assertEqual((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).read_bytes(),data)
        self.assertFalse(await self.scheduler.tick())

    async def test_cancel_during_metadata_enrollment_preserves_originals_and_readonly_retirement_history(self):
        from app.services.federation import protocol as p
        from app.services.node_retirement_history import NativeHistory
        from app.services.node_retirement_master_history import MasterHistory
        for peer in (self.f.source,self.f.target):
            peer.reservations.wall=lambda:self.f.now;peer.observer.wall=lambda:self.f.now
        await self.copying();await self.scheduler.tick();row=await self.task()
        self.assertEqual(row['plan']['execution']['metadata']['objects'],1)
        self.assertIs(row['plan']['execution']['metadata']['complete'],False)
        self.assertFalse(any(path.endswith('/copy') for path in self.control_calls))
        await self.scheduler.cancellation.request(row['task_id'],row['revision'],'admin')
        row=await self.reach('cancelled');self.assertEqual(row['committed_bytes'],0)
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)
        for peer in (self.f.master,self.f.source,self.f.target):
            async with peer.database.connect() as conn:
                before={table.name:[dict(r) for r in (await conn.execute(select(table))).mappings()] for table in s.metadata.tables.values()}
                with patch.object(p,'sign',side_effect=AssertionError('History issued a capability')):
                    result=(await MasterHistory(peer.store,self.f.now).verify(conn,peer.store.node,self.f.source.store.node['node_id'])
                        if peer is self.f.master else await NativeHistory(peer.store,self.f.now).verify(conn,peer.store.node))
                self.assertEqual(result,{'master_tasks' if peer is self.f.master else 'native_tasks':1})
                after={table.name:[dict(r) for r in (await conn.execute(select(table))).mappings()] for table in s.metadata.tables.values()}
                self.assertEqual(before,after)

    async def test_manual_pause_survives_scheduler_restart_and_quiet_traffic(self):
        await self.copying();before=await self.reservations();row=await self.task()
        journal=self.scheduler.journal
        revision=await journal.control(row['task_id'],row['revision'],'pause','admin')
        await self.scheduler.stop();self.scheduler=self.new_scheduler()
        await self.advance_clock(60)
        for _ in range(3):self.assertFalse(await self.scheduler.tick())
        self.assertEqual(((await self.task())['state'],(await self.task())['pause_reason']),('paused','manual'))
        self.assertEqual(await self.reservations(),before)
        await journal.control(row['task_id'],revision,'resume','admin')
        self.assertTrue(await self.scheduler.tick())
        self.assertEqual((await self.task())['state'],'copying')

    async def test_traffic_pause_requires_actual_quiet_interval_and_never_overrides_manual(self):
        await self.copying();self.f.traffic.record(egress=262144)
        self.assertFalse(await self.scheduler.tick());row=await self.task()
        self.assertEqual((row['state'],row['pause_reason']),('paused','traffic'))
        self.clock+=5
        self.assertFalse(await self.scheduler.tick())
        self.clock+=30
        self.assertTrue(await self.scheduler.tick())
        row=await self.task();self.assertEqual(row['state'],'copying')
        await self.scheduler.journal.control(row['task_id'],row['revision'],'pause','admin')
        self.assertFalse(await self.scheduler.tick())
        self.assertEqual((await self.task())['pause_reason'],'manual')

    async def published(self,*,committed=False):
        await self.prepare();await self.reach('committing')
        for _ in range(40):
            await self.scheduler.tick()
            async with self.f.master.database.connect() as conn:
                unit=dict((await conn.execute(select(s.units))).mappings().one())
            expected='committed' if committed else 'published'
            if (unit['state']=='committed' if committed else unit['state']=='publishing' and
                    unit['publication'].get('target_reply',{}).get('state')=='published'):
                self.assertEqual(unit['native_acknowledged'],0)
                return unit
        self.fail('did not reach native '+expected)

    async def paused_phase(self,phase,*,traffic=False):
        if phase=='committing':await self.published()
        else:await self.prepare();await self.reach(phase)
        if phase=='cleanup_pending' and traffic:await self.advance_clock(3601)
        before=await self.reservations();row=await self.task()
        if traffic:
            self.f.traffic.record(egress=262144)
            self.assertFalse(await self.scheduler.tick())
        else:await self.scheduler.journal.control(row['task_id'],row['revision'],'pause','admin')
        await self.scheduler.stop();self.scheduler=self.new_scheduler();count=len(self.control_calls)
        await self.advance_clock(3601)
        for _ in range(3):self.assertFalse(await self.scheduler.tick())
        row=await self.task()
        self.assertEqual((row['state'],row['resume_state'],row['pause_reason']),
            ('paused',phase,'traffic' if traffic else 'manual'))
        self.assertEqual(await self.reservations(),before)
        self.assertEqual(len(self.control_calls),count)
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)
        if traffic:
            self.clock+=5
            self.assertFalse(await self.scheduler.tick())
            self.clock+=30
            self.assertTrue(await self.scheduler.tick())
        else:await self.scheduler.journal.control(row['task_id'],row['revision'],'resume','admin')
        row=await self.reach('cleanup_pending')
        await self.advance_clock(3601)
        await self.reach('completed')
        for _,path,data in self.resources:
            self.assertFalse(path.exists())
            self.assertEqual((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).read_bytes(),data)

    async def test_published_parent_manual_pause_restart_keeps_originals_and_resumes_same_journal(self):
        await self.paused_phase('committing')

    async def test_due_cleanup_manual_pause_remains_paused_after_restart_and_quiet_interval(self):
        await self.paused_phase('cleanup_pending')

    async def test_published_parent_traffic_pause_resumes_only_after_actual_quiet_interval(self):
        await self.paused_phase('committing',traffic=True)

    async def test_cleanup_traffic_pause_retains_all_capacity_and_native_originals(self):
        await self.paused_phase('cleanup_pending',traffic=True)

    async def test_light_native_acknowledgement_precedes_heavy_work_traffic_pause(self):
        unit=await self.published(committed=True)
        self.f.traffic.record(egress=262144)
        for _ in range(2):
            self.assertTrue(await self.scheduler.tick())
            self.assertEqual((await self.task())['state'],'committing')
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.units.c.native_acknowledged).where(
                s.units.c.unit_id==unit['unit_id'])),1)
        self.assertFalse(await self.scheduler.tick())
        row=await self.task()
        self.assertEqual((row['state'],row['resume_state'],row['pause_reason']),('paused','committing','traffic'))
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)

    async def test_pause_during_lost_publication_reply_reconciles_same_native_parent(self):
        await self.prepare();await self.reach('committing');before=await self.reservations()
        original=self.f.transport.request;paused=False
        async def lost_reply(origin,path,**kwargs):
            nonlocal paused
            result=await original(origin,path,**kwargs)
            if path.endswith('/publish-unit') and result.get('payload',{}).get('phase')=='published' and not paused:
                row=await self.task()
                await self.scheduler.journal.control(row['task_id'],row['revision'],'pause','admin')
                paused=True
                raise httpx.ReadTimeout('controlled lost publication reply')
            return result
        self.f.transport.request=lost_reply
        try:
            for _ in range(40):
                await self.scheduler.tick()
                if paused:break
        finally:self.f.transport.request=original
        self.assertTrue(paused)
        row=await self.task();self.assertEqual((row['state'],row['pause_reason']),('paused','manual'))
        self.assertEqual(await self.reservations(),before)
        await self.scheduler.stop();self.scheduler=self.new_scheduler()
        self.assertFalse(await self.scheduler.tick())
        await self.scheduler.journal.control(row['task_id'],row['revision'],'resume','admin')
        await self.reach('cleanup_pending');await self.advance_clock(3601);await self.reach('completed')
        for _,path,data in self.resources:
            self.assertFalse(path.exists())
            self.assertEqual((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).read_bytes(),data)

    async def test_lost_network_reply_retains_capacity_and_retries_with_new_epoch(self):
        await self.ready_to_copy();before=await self.reservations();old=self.scheduler.lease
        with patch.object(self.scheduler.transfer,'copy_one',new=AsyncMock(side_effect=httpx.ReadTimeout('private token forbidden'))):
            self.assertFalse(await self.scheduler.tick())
        row=await self.task();self.assertEqual((row['state'],row['error_code']),('copying','migration_peer_timeout'))
        self.assertEqual(await self.reservations(),before)
        self.assertFalse(await self.scheduler.tick())
        await self.advance_clock(11);self.assertTrue(await self.scheduler.tick())
        self.assertGreater(self.scheduler.lease.epoch,old.epoch)
        self.assertEqual((await self.task())['error_code'],'')
        async with self.f.master.database.connect() as conn:
            audit=[dict(row) for row in (await conn.execute(select(fs.audit))).mappings()]
        self.assertNotIn('private token forbidden',json.dumps(audit,default=str))

    async def test_offline_peer_recovers_automatically_without_approving_a_changed_identity(self):
        await self.copying()
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.relationships).where(fs.relationships.c.peer_id==self.f.source.store.node['node_id']).values(status='offline'))
        self.assertFalse(await self.scheduler.tick())
        self.assertEqual(((await self.task())['state'],(await self.task())['error_code']),('copying','migration_transfer_peer_offline'))
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.relationships).values(status='online'))
        await self.advance_clock(11)
        self.assertTrue(await self.scheduler.tick())
        self.assertEqual((await self.task())['error_code'],'')

    async def test_busy_preparation_does_not_starve_cancellation_or_spin_while_traffic_is_high(self):
        await self.prepare();row=await self.task()
        await self.scheduler.cancellation.request(row['task_id'],row['revision'],'admin')
        other=uuid.uuid4().hex
        async with self.f.master.database.begin() as conn:
            await conn.execute(insert(s.tasks).values(task_id=other,master_id=self.f.master.store.node['node_id'],
                source_id='b'*32,target_id='c'*32,source_relationship='d'*32,target_relationship='e'*32,
                plan_sha256='f'*64,plan={},state='prepared',object_count=1,total_bytes=1,created_at=0,updated_at=0))
        self.f.traffic.record(egress=262144)
        self.assertFalse(await self.scheduler.tick())
        task=await self.task();self.assertEqual((task['state'],task['error_code']),('cancelling','migration_waiting_low_traffic'))
        count=len(self.control_calls)
        self.assertFalse(await self.scheduler.tick());self.assertEqual(len(self.control_calls),count)
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)

    async def test_real_master_heartbeat_is_not_blocked_by_a_pending_transfer_control(self):
        from app.services.federation import runtime as runtime_module
        from app.services.federation import protocol as p
        await self.ready_to_copy();entered=asyncio.Event();pending=asyncio.Event();original=self.f.transport.request
        async def blocked(origin,path,**kwargs):
            if path.endswith('/copy'):entered.set();await pending.wait()
            return await original(origin,path,**kwargs)
        self.f.transport.request=blocked
        runtime=runtime_module.Runtime()
        async with self.f.master.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships).where(
                fs.relationships.c.peer_id==self.f.source.store.node['node_id']))).mappings().one())
        with (patch.object(runtime_module,'state',self.f.master.store),
                patch.object(runtime,'call',new=AsyncMock(return_value={'protocol':p.PROTOCOL_VERSION})) as call,
                patch.object(runtime,'schedule_backup')):
            self.scheduler.start();await asyncio.wait_for(entered.wait(),2)
            await asyncio.wait_for(runtime.tick(relation),2)
            call.assert_awaited_once()
            await self.scheduler.stop()
        async with self.f.master.database.connect() as conn:
            status=await conn.scalar(select(fs.relationships.c.status).where(fs.relationships.c.peer_id==self.f.source.store.node['node_id']))
        self.assertEqual(status,'online')

    async def test_integrity_failure_stops_until_explicit_retry_without_discarding_resources(self):
        await self.ready_to_copy();before=await self.reservations()
        with patch.object(self.scheduler.transfer,'copy_one',new=AsyncMock(side_effect=TransferError('migration_transfer_staging_identity_changed'))) as failed:
            self.assertFalse(await self.scheduler.tick())
            self.assertFalse(await self.scheduler.tick());failed.assert_awaited_once()
        row=await self.task();self.assertEqual(row['state'],'failed')
        self.assertEqual(row['plan']['execution_failure']['state'],'copying')
        self.assertEqual(await self.reservations(),before)
        await self.scheduler.journal.control(row['task_id'],row['revision'],'retry','admin')
        self.assertTrue(await self.scheduler.tick())
        self.assertEqual((await self.task())['state'],'copying')

    async def test_live_foreign_runner_is_not_claimed_or_expired_by_stop(self):
        await self.prepare();await self.scheduler.transfer.preflight(self.plan['plan_id'],'admin')
        foreign=await Journal(self.f.master.store,clock=lambda:self.f.now).claim(self.plan['plan_id'],uuid.uuid4().hex,ttl=60)
        self.assertFalse(await self.scheduler.tick());await self.scheduler.stop()
        async with self.f.master.database.connect() as conn:
            row=dict((await conn.execute(select(s.runners))).mappings().one())
        self.assertEqual((row['runner_id'],row['epoch']),(foreign.runner_id,foreign.epoch))
        self.assertGreater(row['expires_at'],self.f.now)
        await self.advance_clock(61);self.assertTrue(await self.scheduler.tick())
        self.assertGreater(self.scheduler.lease.epoch,foreign.epoch)

    async def test_grace_wait_does_not_prevent_an_independent_queued_task(self):
        await self.prepare();await self.reach('cleanup_pending')
        other=uuid.uuid4().hex
        async with self.f.master.database.begin() as conn:
            await conn.execute(insert(s.tasks).values(task_id=other,master_id=self.f.master.store.node['node_id'],
                source_id='b'*32,target_id='c'*32,source_relationship='d'*32,target_relationship='e'*32,
                plan_sha256='f'*64,plan={},state='queued',object_count=1,total_bytes=1,created_at=1,updated_at=1))
        self.assertTrue(await self.scheduler.tick())
        self.assertEqual(self.scheduler.lease.task_id,other)
        self.assertEqual((await self.task(other))['state'],'copying')
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)

    async def test_stop_joins_active_step_and_keeps_all_capacity_and_owned_fences(self):
        await self.ready_to_copy();before=await self.reservations();entered=asyncio.Event();blocked=asyncio.Event()
        async def waiting(*_args):entered.set();await blocked.wait()
        with patch.object(self.scheduler.transfer,'copy_one',new=AsyncMock(side_effect=waiting)):
            self.scheduler.start();await asyncio.wait_for(entered.wait(),2)
            await self.scheduler.stop()
        self.assertIsNone(self.scheduler.task);self.assertIsNone(self.scheduler.lease)
        self.assertEqual(await self.reservations(),before)
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.runners.c.expires_at)),0)
            self.assertGreater(await conn.scalar(select(func.count()).select_from(s.scopes)),0)
