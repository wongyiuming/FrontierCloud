"""Three actual SQL stores and native files; grace clock is explicitly virtual."""
import os
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete,func,insert,select,text,update

from app.services import media_crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_finalize_master import Finalization
from app.services.resource_finalize_peers import FinalizePeers
from app.services.resource_migration_journal import Conflict
from app.services.resource_observation import digest
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_source_cleanup as fixtures


@unittest.skipUnless(os.name=='posix','final census requires native nofollow I/O')
class FinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.SourceCleanupTests('runTest');await self.fixture.asyncSetUp();self.f=self.fixture.f
        self.original=self.f.transport.request;self.f.transport.request=self.control;self.after=None
        self.service=Finalization(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def asyncTearDown(self):await self.fixture.asyncTearDown()

    async def control(self,origin,path,**kwargs):
        if '/finalize-' not in path:return await self.original(origin,path,**kwargs)
        side=path.rsplit('-',1)[-1];self.assertIn(side,('source','target'));peer=getattr(self.f,side)
        self.assertEqual(origin,peer.store.node['endpoint']);self.assertEqual((kwargs['credential'],kwargs['relation']),(peer.credential,peer.relationship))
        async with peer.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==peer.relationship))).mappings().one())
        result=await getattr(FinalizePeers(peer.transfers),side)(peer.store.node,relation,**kwargs['value'])
        if self.after:await self.after(side,result)
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)

    async def prepare(self):
        await self.fixture.prepare();await self.fixture.finish();self.lease=self.fixture.lease;self.resources=self.fixture.resources

    async def step(self):return await self.service.advance(self.lease)

    async def finish(self):
        for _ in range(30):
            result=await self.step()
            if result['task_completed']:return result
        self.fail('Final censuses did not converge')

    async def source_released(self):
        for _ in range(15):
            result=await self.step()
            if result['phase']=='source_released':return result
        self.fail('Source final census did not converge')

    async def states(self):
        result={}
        for side in ('source','target'):
            async with getattr(self.f,side).database.connect() as conn:
                result[side]=await conn.scalar(select(s.peer_reservations.c.state))
        async with self.f.master.database.connect() as conn:result['master']=await conn.scalar(select(s.tasks.c.state))
        return result

    async def test_complete_censuses_release_only_owned_fences_keep_history_and_business(self):
        await self.prepare()
        async with self.f.source.database.begin() as conn:
            # A native cache is not authoritative after replica removal. Only
            # the complete physical/registry census may reconcile it to zero.
            await conn.execute(update(fs.storage_members).values(used_bytes=43))
        async with self.f.master.database.connect() as conn:
            before=[dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()]
        async with self.f.target.database.begin() as conn:
            await conn.execute(update(fs.storage_members).values(used_bytes=fs.storage_members.c.used_bytes+19,reserved_bytes=23))
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read payload'))):
            result=await self.finish()
        self.assertTrue(result['task_completed']);self.assertEqual(set(result['completion']['receipts']),{'source','target'})
        self.assertEqual(result['completion']['receipts']['source']['payload']['native_used_before'],43)
        self.assertEqual(await self.states(),dict(source='released',target='released',master='completed'))
        await self.fixture.target_preserved()
        for side in ('source','target','master'):
            peer=getattr(self.f,side)
            async with peer.database.connect() as conn:
                for table in (s.scopes,s.capacity,s.drains):self.assertEqual(await conn.scalar(select(func.count()).select_from(table)),0)
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.units)),1)
                if side!='master':self.assertEqual(await conn.scalar(select(func.count()).select_from(s.storage_journal)),len(self.resources))
                if side=='source':self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),0)
                if side=='target':
                    self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),sum(len(data) for _,_,data in self.resources)+19)
                    self.assertEqual(await conn.scalar(select(fs.storage_members.c.reserved_bytes)),23)
            if side=='target':self.assertFalse((peer.roots['media']/'.frontiercloud-migration'/self.lease.task_id).exists())
        async with self.f.master.database.connect() as conn:
            self.assertEqual(before,[dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()])
            self.assertEqual(await conn.scalar(select(s.runners.c.expires_at)),0)
            self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),sum(len(data) for _,_,data in self.resources))

    async def test_source_replay_after_new_usage_is_readonly_and_completion_replay_is_stable(self):
        await self.prepare();first=await self.finish()
        async with self.f.master.database.connect() as conn:task=dict((await conn.execute(select(s.tasks))).mappings().one())
        relations=await self.service.t.relations(task);grant=self.service.t._grant(task,relations,self.lease.epoch+1,uuid.uuid4().hex,'finalize')
        peer=self.f.source
        async with peer.database.begin() as conn:
            await conn.execute(update(fs.storage_members).values(used_bytes=123,reserved_bytes=17))
            relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            stored=await conn.scalar(select(s.peer_reservations.c.payload))
        with patch.object(self.f.traffic,'snapshot',return_value=dict(can_run=False,readers_clear=False)):
            replay=await FinalizePeers(peer.transfers).source(peer.store.node,relation,uuid.uuid4().hex,grant)
        self.assertEqual(replay['receipt'],stored['final_receipt'])
        async with peer.database.connect() as conn:
            self.assertEqual(tuple((await conn.execute(select(fs.storage_members.c.used_bytes,fs.storage_members.c.reserved_bytes))).one()),(123,17))
            self.assertEqual(await conn.scalar(select(s.peer_reservations.c.payload)),stored)
        self.assertEqual((await self.step())['completion'],first['completion'])

    async def test_unknown_file_outside_admitted_parent_blocks_final_census_and_is_preserved(self):
        await self.prepare();unknown=self.f.source.roots['media']/'outside.bin';unknown.write_bytes(b'owner data')
        with self.assertRaisesRegex(TransferError,'source_resources_remain'):await self.finish()
        self.assertEqual(unknown.read_bytes(),b'owner data')
        self.assertEqual(await self.states(),dict(source='active',target='active',master='cleanup_pending'))
        await self.fixture.target_preserved()

    async def test_replaced_empty_source_root_cannot_fake_a_successful_final_census(self):
        await self.prepare();root=self.f.source.roots['media'];old=root.with_name('original-media');root.rename(old);root.mkdir()
        with self.assertRaisesRegex(TransferError,'source_root_changed'):await self.finish()
        self.assertTrue(old.is_dir());self.assertTrue(root.is_dir())
        self.assertEqual((await self.states())['source'],'active')

    async def test_live_reader_and_foreign_reservation_block_source_final_release(self):
        await self.prepare();await self.step();self.f.traffic.entered()
        try:
            with self.assertRaisesRegex(TransferError,'waiting_reader'):await self.step()
        finally:self.f.traffic.exited()
        peer=self.f.source
        async with peer.database.begin() as conn:
            await conn.execute(insert(s.capacity).values(task_id='a'*32,member_id=peer.store.node['node_id'],reserved_bytes=0,updated_at=int(self.f.now)))
        with self.assertRaisesRegex(TransferError,'foreign_work'):await self.step()
        async with peer.database.begin() as conn:await conn.execute(delete(s.capacity).where(s.capacity.c.task_id=='a'*32))
        await self.finish()

    async def test_source_final_audit_failure_rolls_back_usage_and_release_then_retries(self):
        await self.prepare();original=self.f.source.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_native_finalized':raise OSError('injected Source final audit failure')
            return await original(conn,action,actor,**kwargs)
        with patch.object(self.f.source.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.finish()
        self.assertEqual((await self.states())['source'],'active')
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),1)
            self.assertIsNone((await conn.scalar(select(s.peer_reservations.c.payload))).get('final_receipt'))
        await self.finish()

    async def test_lost_source_final_reply_and_new_runner_recover_without_erasing_new_counters(self):
        await self.prepare()
        async def lose(side,result):
            if side=='source' and result['phase']=='released':self.after=None;raise TimeoutError('injected lost final receipt')
        self.after=lose
        with self.assertRaises(TimeoutError):await self.finish()
        self.assertEqual((await self.states())['source'],'released')
        await self.fixture.advance_clock(61);self.lease=self.fixture.lease
        old=self.f.source.transfers;await old.close()
        replacement=TransferPeers(self.f.source.store,root_provider=lambda:self.f.source.roots,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.fixture.fixture.replacements.append(replacement);self.f.source.transfers=replacement
        await self.finish();await self.fixture.target_preserved()

    async def test_target_change_after_source_release_keeps_master_and_target_exclusions(self):
        await self.prepare();await self.source_released()
        original=self.resources[0][1];root=self.f.target.roots['media'];target=root/original.relative_to(self.f.source.roots['media'])
        target.write_bytes(b'x'*target.stat().st_size)
        with self.assertRaisesRegex(TransferError,'file_changed'):await self.finish()
        self.assertEqual(await self.states(),dict(source='released',target='active',master='cleanup_pending'))
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.drains)),1)

    async def test_master_final_audit_failure_retains_fences_and_uses_durable_peer_receipts(self):
        await self.prepare();original=self.f.master.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_completed':raise OSError('injected Master final audit failure')
            return await original(conn,action,actor,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.finish()
        self.assertEqual(await self.states(),dict(source='released',target='released',master='cleanup_pending'))
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),2)
        await self.finish()

    async def test_forged_boolean_receipt_is_refused_without_completing_master(self):
        await self.prepare()
        async def corrupt(side,result):
            if side=='source' and result['phase']=='released':
                bad={**result['receipt']['payload'],'epoch':True}
                result['receipt']=p.sign(self.f.source.store.unseal(self.f.source.store.node['private_key']),bad)
        self.after=corrupt
        with self.assertRaisesRegex(TransferError,'receipt_invalid'):await self.finish()
        self.assertEqual((await self.states())['master'],'cleanup_pending');self.after=None;await self.finish()

    async def test_cleaned_units_cannot_bypass_the_terminal_censuses_via_checkpoint(self):
        await self.prepare()
        with self.assertRaisesRegex(Conflict,'最终盘点'):
            await self.service.t.journal.checkpoint(self.lease,'completed',committed_bytes=sum(len(data) for _,_,data in self.resources))
        self.assertEqual((await self.states())['master'],'cleanup_pending')

    async def test_unknown_private_target_staging_is_preserved_and_blocks_release(self):
        await self.prepare();await self.source_released()
        private=self.f.target.roots['media']/'.frontiercloud-migration'/self.lease.task_id
        self.assertTrue(private.is_dir());unknown=private/'owner-private.bin';unknown.write_bytes(b'keep owner material')
        with self.assertRaisesRegex(TransferError,'unknown_stage'):await self.finish()
        self.assertEqual(unknown.read_bytes(),b'keep owner material')
        self.assertEqual(await self.states(),dict(source='released',target='active',master='cleanup_pending'))
        # Resolution is an explicit fixture-owner action; the product never
        # invents an ownership receipt for this unknown file or removes it.
        retained=self.f.root/'owner-preserved-stage.bin';unknown.rename(retained)
        await self.finish();self.assertEqual(retained.read_bytes(),b'keep owner material')

    async def test_a_previously_missing_recording_root_cannot_silently_change_identity(self):
        recordings=self.f.source.roots['recordings']
        if recordings.exists():recordings.rmdir()
        await self.prepare();recordings.mkdir()
        with self.assertRaisesRegex(TransferError,'source_root_changed'):await self.finish()
        self.assertTrue(recordings.is_dir());self.assertEqual((await self.states())['source'],'active')
