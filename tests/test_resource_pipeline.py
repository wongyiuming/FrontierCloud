"""Native per-parent pipeline and metadata enrollment; time is virtual here."""
import os
import unittest
import uuid

from sqlalchemy import event,func,select

from app.services import resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.resource_publish_peers import PublishPeers
from app.services.resource_transfer_enrollment import Enrollment
from app.services.resource_transfer_protocol import WINDOW_BYTES,TransferError
from tests import test_resource_scheduler as fixtures


@unittest.skipUnless(os.name=='posix','requires native nofollow paths')
class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.x=fixtures.SchedulerTests('runTest');await self.x.asyncSetUp();self.f=self.x.f

    async def asyncTearDown(self):await self.x.asyncTearDown()

    async def enrolled(self):
        await self.x.copying()
        for _ in range(12):
            await self.x.scheduler.tick()
            row=await self.x.task()
            if row['plan']['execution'].get('metadata',{}).get('complete'):return row
            self.assertNotEqual(row['state'],'failed',row['error_code'])
        self.fail('metadata enrollment did not converge')

    async def test_complete_roster_is_registered_without_copying_or_fabricating_verification(self):
        counts=[]
        def observe(_connection,_cursor,statement,*_args):
            if 'count(' in statement.lower() and 'migration_peer_bindings' in statement.lower():counts.append(statement)
        engines=[peer.database.sync_engine for peer in (self.f.source,self.f.target)]
        for engine in engines:event.listen(engine,'before_cursor_execute',observe)
        try:row=await self.enrolled()
        finally:
            for engine in engines:event.remove(engine,'before_cursor_execute',observe)
        self.assertLessEqual(len(counts),4,'enrollment must not recount the growing task/unit roster for every object')
        self.assertEqual((row['plan']['execution']['metadata']['objects'],row['plan']['execution']['metadata']['bytes']),
            (row['object_count'],row['total_bytes']))
        for peer in (self.f.source,self.f.target):
            async with peer.database.connect() as conn:
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.peer_bindings)),row['object_count'])
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.storage_journal)),0)
            self.assertFalse(list(peer.roots['media'].rglob('*.part')))
        async with self.f.master.database.connect() as conn:
            rows=(await conn.execute(select(s.objects))).mappings().all()
        self.assertTrue(all(r['state']=='pending' and r['verified_bytes']==0 and r['target_sha256']=='' for r in rows))
        self.assertFalse(any(path.endswith('/copy') for path in self.x.control_calls))

    async def test_lost_target_registration_reply_replays_metadata_without_duplicate_bindings(self):
        await self.x.copying();transfer=self.x.scheduler.transfer;lease=self.x.scheduler.lease
        original=self.f.transport.request;lost=False
        async def request(origin,path,**kwargs):
            nonlocal lost
            result=await original(origin,path,**kwargs)
            if path.endswith('/register') and origin==self.f.target.store.node['endpoint'] and not lost:
                lost=True;raise TimeoutError('injected lost registration response')
            return result
        self.f.transport.request=request
        with self.assertRaises(TimeoutError):await Enrollment(transfer).advance(lease)
        self.f.transport.request=original
        self.assertFalse(await Enrollment(transfer).advance(lease))
        row=await self.x.task();self.assertEqual(row['plan']['execution']['metadata']['objects'],1)
        for peer in (self.f.source,self.f.target):
            async with peer.database.connect() as conn:
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.peer_bindings)),1)
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.storage_journal)),0)

    async def test_registration_cannot_be_used_as_a_source_byte_capability(self):
        await self.enrolled();row=await self.x.task();transfer=self.x.scheduler.transfer
        async with self.f.master.database.begin() as conn:
            logical=await conn.scalar(select(s.objects.c.logical_id).limit(1))
            obj=await transfer._object(conn,row,logical)
        relations=await transfer.relations(row)
        grant=transfer._grant(row,relations,self.x.scheduler.lease.epoch,self.x.scheduler.lease.runner_id,'register',obj)
        with self.assertRaisesRegex(TransferError,'purpose_invalid'):
            await self.f.source.transfers.source(dict(grant=grant,proof={}))
        async with self.f.target.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
        with self.assertRaisesRegex(TransferError,'grant_invalid'):
            await self.f.target.transfers.register(self.f.target.store.node,relation,uuid.uuid4().hex,dict(payload=None))
        for peer in (self.f.source,self.f.target):
            async with peer.database.connect() as conn:
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.storage_journal)),0)

    async def test_first_parent_commits_before_next_copy_and_restart_continues_the_same_pipeline(self):
        self.x.resources=[await self.f.media(b'first original'*400,path='music/one/one.mp3'),
            await self.f.media(b'long original'+b'part'*(WINDOW_BYTES//2),path='music/two/two.mp3',encrypted=True)]
        self.x.plan=await self.f.plan();self.assertEqual(self.x.plan['state'],'ready',self.x.plan)
        from app.services.resource_migration_admission import Admission
        await Admission(self.f.master.store,self.f.transport,preview_epoch=self.f.planner.epoch,
            traffic=self.f.traffic,wall=lambda:self.f.now).prepare(self.x.plan['plan_id'],self.x.plan['review_sha256'],'admin')
        first=None;whole_roster_counts=[]
        def observe(_connection,_cursor,statement,*_args):
            sql=statement.lower()
            if 'count(' in sql and 'from migration_objects' in sql and 'migration_objects.unit_id' not in sql:
                whole_roster_counts.append(statement)
        engine=self.f.master.database.sync_engine;event.listen(engine,'before_cursor_execute',observe)
        try:
            for _ in range(200):
                await self.x.scheduler.tick();task=await self.x.task()
                self.assertNotEqual(task['state'],'failed',task['error_code'])
                async with self.f.master.database.connect() as conn:
                    units=(await conn.execute(select(s.units).order_by(s.units.c.unit_id))).mappings().all()
                    rows=(await conn.execute(select(s.objects))).mappings().all()
                if units[0]['state']=='committed' and units[0]['native_acknowledged']==1:
                    first=dict(units[0]);break
        finally:event.remove(engine,'before_cursor_execute',observe)
        self.assertLessEqual(len(whole_roster_counts),1,'publication must not recount every task object for each parent/metadata page')
        self.assertIsNotNone(first)
        self.assertEqual(units[1]['state'],'pending')
        future=[r for r in rows if r['unit_id']==units[1]['unit_id']]
        self.assertTrue(future and all(r['state']=='pending' and r['verified_bytes']==0 for r in future))
        self.assertEqual(task['committed_bytes'],first['total_bytes'])
        for logical,path,data in self.x.resources:
            target=self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])
            self.assertEqual(path.read_bytes(),data)
            if path.parent.name==first['parent_path'].rsplit('/',1)[-1]:self.assertEqual(target.read_bytes(),data)
            else:self.assertFalse(target.exists())
        await self.x.scheduler.stop();self.x.scheduler=self.x.new_scheduler()
        await self.x.reach('cleanup_pending')
        await self.x.advance_clock(3601)
        row=await self.x.reach('completed')
        self.assertEqual(row['committed_bytes'],row['total_bytes'])
        for logical,path,data in self.x.resources:
            self.assertFalse(path.exists())
            self.assertEqual((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).read_bytes(),data)

    async def test_incomplete_current_parent_cannot_publish_even_with_all_metadata_registered(self):
        await self.enrolled()
        with self.assertRaisesRegex(TransferError,'copies_incomplete'):
            await self.x.scheduler.publication.enter(self.x.scheduler.lease)
        row=await self.x.task();self.assertEqual(row['state'],'copying');self.assertEqual(row['committed_bytes'],0)
        relations=await self.x.scheduler.transfer.relations(row)
        grant=self.x.scheduler.transfer._grant(row,relations,self.x.scheduler.lease.epoch,self.x.scheduler.lease.runner_id,'publish')
        async with self.f.target.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            unit=await conn.scalar(select(s.units.c.unit_id).order_by(s.units.c.unit_id).limit(1))
        with self.assertRaisesRegex(TransferError,'metadata_incomplete'):
            await PublishPeers(self.f.target.transfers).advance(self.f.target.store.node,relation,uuid.uuid4().hex,grant,unit)
        await self.x.reach('cleanup_pending')
