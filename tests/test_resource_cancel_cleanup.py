"""Mixed-task cleanup boundaries with virtual time; no elapsed-hour claim."""
import os
import unittest
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import delete,func,insert,select,update

from app.services import media_crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cleanup_master import Cleanup
from app.services.resource_migration_journal import Conflict
from app.services.resource_source_cleanup import SourceCleanup
from app.services.resource_target_verify import TargetVerification
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_master as fixtures


@unittest.skipUnless(os.name=='posix','mixed cleanup requires native Linux files')
class MixedCancellationCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case=fixtures.MasterCancellationVariantsTests('runTest');await self.case.asyncSetUp();await self.case.mixed();await self.case.finish()
        self.f,self.lease,self.unit,self.resources=self.case.f,self.case.lease,self.case.committed,self.case.resources
        self.extra=[];original=self.f.transport.request;self.calls=[]
        async def request(origin,path,**kwargs):
            if path.endswith('/verify-target'):peer,service=self.f.target,TargetVerification
            elif path.endswith('/cleanup-source'):peer,service=self.f.source,SourceCleanup
            else:return await original(origin,path,**kwargs)
            self.assertEqual((origin,kwargs['credential'],kwargs['relation']),(peer.store.node['endpoint'],peer.credential,peer.relationship))
            async with peer.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            self.calls.append((path,kwargs['value']))
            result=await service(peer.transfers).advance(peer.store.node,relation,**kwargs['value'])
            return p.sign(peer.store.unseal(peer.store.node['private_key']),result)
        self.f.transport.request=request;self.service=self.new_service()

    async def asyncTearDown(self):
        for item in self.extra:await item.close()
        await self.case.asyncTearDown()

    def new_service(self):return Cleanup(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def clock(self,seconds):
        self.f.now+=seconds
        async with self.f.master.database.begin() as conn:await conn.execute(update(fs.relationships).values(last_heartbeat=int(self.f.now)))
        self.lease=await self.service.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)

    async def step(self):
        self.lease,result=await self.service.advance(self.lease,self.unit)
        return result

    async def ready(self):
        for _ in range(25):
            result=await self.step()
            if result['phase']=='ready':return result
        self.fail('mixed Source cleanup verification did not converge')

    async def finish(self):
        for _ in range(45):
            result=await self.step()
            if result['source_cleaned']:return result
        self.fail('mixed Source replica cleanup did not converge')

    async def retained(self):
        for _,path,data in self.resources[1:]:self.assertEqual(path.read_bytes(),data)
        _,path,data=self.resources[0]
        self.assertEqual((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).read_bytes(),data)

    async def test_only_committed_replica_is_removed_without_completing_or_reconciling_usage(self):
        await self.clock(3601)
        async with self.f.master.database.connect() as conn:before=[dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()]
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted media')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read payload'))):result=await self.finish()
        self.assertEqual((result['deleted_count'],result['deleted_bytes']),(1,len(self.resources[0][2])))
        self.assertFalse(result['task_completed']);self.assertFalse(self.resources[0][1].exists());await self.retained()
        for peer in (self.f.master,self.f.source):
            async with peer.database.connect() as conn:
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.drains)),1)
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),2 if peer is self.f.master else 1)
        async with self.f.master.database.connect() as conn:
            self.assertEqual([dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()],before)
            self.assertEqual(await conn.scalar(select(s.tasks.c.state)),'cancelling')
            self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),len(self.resources[0][2]))
            self.assertEqual(set((await conn.execute(select(s.units.c.state))).scalars()),{'cleaned','cancelled'})
        async with self.f.source.database.connect() as conn:
            self.assertEqual(set((await conn.execute(select(s.storage_journal.c.logical_id))).scalars()),{self.resources[0][0]})
            self.assertEqual(await conn.scalar(select(s.storage_journal.c.state)),'deleted')

    async def test_original_hour_grace_cannot_be_shortened_by_cancellation(self):
        result=await self.step();self.assertEqual(result['phase'],'waiting_grace');self.assertTrue(self.resources[0][1].exists())
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.units).where(s.units.c.unit_id==self.unit).values(cleanup_after=int(self.f.now)))
        with self.assertRaisesRegex(TransferError,'grace_record_changed'):await self.step()
        self.assertTrue(self.resources[0][1].exists());await self.retained()

    async def test_cancelled_parent_cannot_be_selected_for_source_deletion(self):
        await self.clock(3601)
        async with self.f.master.database.connect() as conn:cancelled=await conn.scalar(select(s.units.c.unit_id).where(s.units.c.state=='cancelled'))
        with self.assertRaisesRegex(TransferError,'unit_not_committed'):await self.service.advance(self.lease,cancelled)
        self.assertEqual(self.calls,[]);self.assertTrue(self.resources[0][1].exists());await self.retained()

    async def test_unsettled_cancelled_parent_blocks_cleanup_on_master(self):
        await self.clock(3601)
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.units).where(s.units.c.state=='cancelled').values(native_acknowledged=0))
        with self.assertRaisesRegex(TransferError,'all_native_acknowledgements_required'):await self.step()
        self.assertTrue(self.resources[0][1].exists());await self.retained()

    async def test_unsettled_cancelled_parent_blocks_cleanup_on_native_source(self):
        await self.clock(3601)
        async with self.f.source.database.begin() as conn:await conn.execute(update(s.units).where(s.units.c.state=='cancelled').values(native_acknowledged=0))
        with self.assertRaisesRegex(TransferError,'all_native_commits_required'):await self.finish()
        self.assertTrue(self.resources[0][1].exists());await self.retained()

    async def test_missing_native_cancel_fence_does_not_treat_cancelled_units_as_committed(self):
        await self.clock(3601)
        async with self.f.source.database.begin() as conn:
            payload=await conn.scalar(select(s.peer_reservations.c.payload))
            await conn.execute(update(s.peer_reservations).values(payload={**payload,'transfer':{**payload['transfer'],'cancel_started':False}}))
        with self.assertRaisesRegex(TransferError,'all_native_commits_required'):await self.finish()
        self.assertTrue(self.resources[0][1].exists());await self.retained()

    async def test_live_http_and_durable_source_readers_still_block_unlink(self):
        await self.clock(3601);await self.ready();self.f.traffic.entered()
        try:self.assertEqual((await self.step())['phase'],'waiting_reader');self.assertTrue(self.resources[0][1].exists())
        finally:self.f.traffic.exited()
        async with self.f.source.database.begin() as conn:
            await conn.execute(insert(s.read_leases).values(lease_id='a'*32,member_id=self.f.source.store.node['node_id'],logical_id=self.resources[0][0],
                object_id=self.resources[0][0],source_version='b'*64,expires_at=int(self.f.now)+300,task_id='',purpose='playback'))
        self.assertEqual((await self.step())['phase'],'waiting_reader');self.assertTrue(self.resources[0][1].exists())
        async with self.f.source.database.begin() as conn:await conn.execute(delete(s.read_leases).where(s.read_leases.c.lease_id=='a'*32))
        await self.finish();await self.retained()

    async def test_target_damage_refuses_cleanup_and_preserves_the_source_replica(self):
        await self.clock(3601);_,path,_=self.resources[0]
        target=self.f.target.roots['media']/path.relative_to(self.f.source.roots['media']);target.write_bytes(b'preserve changed Target file')
        with self.assertRaisesRegex(TransferError,'migration_publish_file_changed'):await self.finish()
        self.assertEqual(path.read_bytes(),self.resources[0][2]);self.assertEqual(target.read_bytes(),b'preserve changed Target file')

    async def test_native_audit_failure_after_unlink_reconciles_only_owned_replica(self):
        await self.clock(3601);await self.ready();log=self.f.source.store.log
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_source_replica_removed':raise OSError('Controlled Source audit rollback after unlink')
            return await log(conn,action,*args,**kwargs)
        with patch.object(self.f.source.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.step()
        self.assertFalse(self.resources[0][1].exists());await self.retained()
        await self.f.source.transfers.close()
        self.f.source.transfers=TransferPeers(self.f.source.store,root_provider=lambda:self.f.source.roots,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.extra.append(self.f.source.transfers);self.service=self.new_service()
        await self.finish();await self.retained()

    async def test_lost_source_cleanup_reply_and_new_master_service_do_not_repeat_other_deletions(self):
        await self.clock(3601);original=self.f.transport.request;lost=False
        async def request(origin,path,**kwargs):
            nonlocal lost
            value=await original(origin,path,**kwargs)
            if path.endswith('/cleanup-source') and value['payload']['deleted_count'] and not lost:
                lost=True;raise httpx.ReadTimeout('Controlled lost native cleanup reply')
            return value
        self.f.transport.request=request
        with self.assertRaises(httpx.ReadTimeout):await self.finish()
        self.service=self.new_service();await self.finish();self.assertTrue(lost);await self.retained()

    async def test_external_file_in_cancelled_parent_is_preserved_and_not_treated_as_empty_source(self):
        unknown=self.resources[1][1].with_name('external-owner-data.bin');unknown.write_bytes(b'keep owner data in retained parent')
        await self.clock(3601);await self.finish();await self.retained()
        self.assertEqual(unknown.read_bytes(),b'keep owner data in retained parent')
        async with self.f.source.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.peer_reservations.c.state)),'active')

    async def test_peak_traffic_defers_cleanup_and_quiet_recovery_keeps_cancelled_originals(self):
        await self.clock(3601);await self.ready();self.f.traffic.record(egress=100*1024**2)
        with self.assertRaisesRegex(TransferError,'waiting_low_traffic'):await self.step()
        self.assertTrue(self.resources[0][1].exists());await self.retained()
        self.f.traffic._clock=lambda:1100;self.f.traffic._quiet_since=1000
        await self.finish();await self.retained()

    async def test_new_runner_fences_the_old_cleanup_without_changing_committed_bytes(self):
        await self.clock(3601);await self.ready();old=self.lease;await self.clock(61)
        with self.assertRaises(Conflict):await self.service.advance(old,self.unit)
        await self.finish();await self.retained()
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),len(self.resources[0][2]))
