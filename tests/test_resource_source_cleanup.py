"""Virtual-clock SQL/filesystem fault tests; not real one-hour acceptance."""
import asyncio
import os
import threading
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete,func,insert,select,text,update

from app.services import media_crypto,resource_cleanup_files as files,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cleanup_master import Cleanup
from app.services.resource_migration_journal import Conflict
from app.services.resource_source_cleanup import SourceCleanup
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_target_verify as fixtures


@unittest.skipUnless(os.name=='posix','source cleanup requires native nofollow I/O')
class SourceCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.TargetVerificationTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f;self.original=self.f.transport.request;self.f.transport.request=self.control;self.after=None
        self.service=Cleanup(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def asyncTearDown(self):await self.fixture.asyncTearDown()

    async def control(self,origin,path,**kwargs):
        if not path.endswith('/cleanup-source'):return await self.original(origin,path,**kwargs)
        peer=self.f.source;self.assertEqual(origin,peer.store.node['endpoint'])
        self.assertEqual((kwargs['credential'],kwargs['relation']),(peer.credential,peer.relationship))
        async with peer.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==peer.relationship))).mappings().one())
        result=await SourceCleanup(peer.transfers).advance(peer.store.node,relation,**kwargs['value'])
        if self.after:await self.after(result)
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)

    async def advance_clock(self,seconds):
        # This explicitly controlled unit fixture tests boundaries and crash
        # recovery. Native cleanup acceptance must wait the actual full hour.
        self.f.now+=seconds
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.relationships).values(last_heartbeat=int(self.f.now)))
        self.lease=await self.service.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)

    async def prepare(self,*,grace=True):
        await self.fixture.prepare();self.lease,self.unit=self.fixture.lease,self.fixture.unit
        self.resources=self.fixture.fixture.resources
        if grace:await self.advance_clock(3601)

    async def step(self):
        self.lease,result=await self.service.advance(self.lease,self.unit);return result

    async def ready(self):
        for _ in range(20):
            result=await self.step()
            if result['phase']=='ready':return result
        self.fail('Source cleanup verification did not converge')

    async def finish(self):
        for _ in range(40):
            result=await self.step()
            if result['source_cleaned']:return result
        self.fail('Source cleanup did not converge')

    async def retained(self):
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)

    async def target_preserved(self):
        for _,path,data in self.resources:
            if getattr(self,'kind','media')=='recording':
                target=self.f.target.roots['recordings']/self.f.target.relationship/path.parent.name/path.name
            else:target=self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])
            self.assertEqual(target.read_bytes(),data)

    async def test_original_grace_is_required_and_cannot_be_shortened(self):
        await self.prepare(grace=False);result=await self.step()
        self.assertEqual(result['phase'],'waiting_grace');await self.retained()
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(s.units).values(cleanup_after=int(self.f.now)))
        with self.assertRaisesRegex(TransferError,'grace_record_changed'):await self.step()
        await self.retained()

    async def test_verified_replicas_are_removed_without_keys_or_master_business_changes(self):
        await self.prepare();before=await self.fixture.fixture.members()
        async with self.f.master.database.connect() as conn:
            catalog=[dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()]
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read payload'))):
            result=await self.finish()
        self.assertFalse(result['task_completed']);self.assertEqual(result['deleted_count'],len(self.resources))
        self.assertEqual(result['deleted_bytes'],sum(len(data) for _,_,data in self.resources))
        self.assertTrue(all(not path.exists() for _,path,_ in self.resources));await self.target_preserved()
        self.assertEqual(await self.fixture.fixture.members(),before)
        async with self.f.master.database.connect() as conn:
            self.assertEqual([dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()],catalog)
            self.assertEqual(await conn.scalar(select(s.tasks.c.state)),'cleanup_pending')
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),2)
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM media_objects')),0)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.storage_journal).where(s.storage_journal.c.state=='deleted')),2)
            self.assertEqual(await conn.scalar(select(s.peer_reservations.c.state)),'active')

    async def test_active_http_reader_and_live_durable_lease_both_prevent_deletion(self):
        await self.prepare();await self.ready();self.f.traffic.entered()
        try:self.assertEqual((await self.step())['phase'],'waiting_reader');await self.retained()
        finally:self.f.traffic.exited()
        async with self.f.source.database.begin() as conn:
            await conn.execute(insert(s.read_leases).values(lease_id='d'*32,member_id=self.f.source.store.node['node_id'],
                logical_id=self.resources[0][0],object_id='e'*64,source_version='f'*64,expires_at=int(self.f.now)+300,task_id='',purpose='playback'))
        self.assertEqual((await self.step())['phase'],'waiting_reader');await self.retained()
        async with self.f.source.database.begin() as conn:await conn.execute(delete(s.read_leases))
        await self.finish();await self.target_preserved()

    async def test_unknown_source_file_is_preserved_and_blocks_cleanup(self):
        await self.prepare();unknown=self.resources[0][1].parent/'unknown.bin';unknown.write_bytes(b'keep unknown')
        with self.assertRaisesRegex(TransferError,'unknown_source_file'):await self.finish()
        self.assertEqual(unknown.read_bytes(),b'keep unknown');await self.retained();await self.target_preserved()

    async def test_unlink_then_native_sql_audit_failure_recovers_after_service_restart(self):
        await self.prepare();await self.ready();log=self.f.source.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_source_replica_removed':raise OSError('injected audit failure after unlink')
            return await log(conn,action,actor,**kwargs)
        with patch.object(self.f.source.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.step()
        self.assertEqual(sum(path.exists() for _,path,_ in self.resources),1)
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.storage_journal.c.state)),'deleting')
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM media_objects')),2)
        old=self.f.source.transfers;await old.close()
        replacement=TransferPeers(self.f.source.store,root_provider=lambda:self.f.source.roots,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.fixture.replacements.append(replacement);self.f.source.transfers=replacement
        await self.finish();await self.target_preserved()

    async def test_lost_master_audit_reconciles_cumulative_native_progress(self):
        await self.prepare();await self.ready();log=self.f.master.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_source_cleanup_step':raise OSError('injected lost Master progress')
            return await log(conn,action,actor,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.step()
        self.assertEqual(sum(path.exists() for _,path,_ in self.resources),1)
        await self.finish();await self.target_preserved()

    async def test_changed_file_after_durable_unlink_intent_is_not_removed(self):
        await self.prepare();await self.ready()
        with patch.object(files,'unlink',side_effect=OSError('injected pre-unlink failure')):
            with self.assertRaises(OSError):await self.step()
        async with self.f.source.database.connect() as conn:
            path=await conn.scalar(select(s.storage_journal.c.physical_path))
        native=self.f.source.roots['media']/path;replacement=b'x'*native.stat().st_size;native.write_bytes(replacement)
        with self.assertRaisesRegex(TransferError,'source_file_changed'):await self.step()
        self.assertEqual(native.read_bytes(),replacement);await self.target_preserved()

    async def test_changed_master_ownership_refuses_source_cleanup_before_any_intent(self):
        await self.prepare()
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.global_media).values(storage_member_id=self.f.source.store.node['node_id']))
        with self.assertRaisesRegex(TransferError,'master_catalog_changed'):await self.step()
        await self.retained()
        async with self.f.source.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.units.c.state)),'committed')

    async def test_new_runner_fences_old_cleanup_calls_without_rewinding_progress(self):
        await self.prepare();await self.ready();old=self.lease
        await self.advance_clock(61)
        with self.assertRaises(Conflict):await self.service.advance(old,self.unit)
        await self.finish();await self.target_preserved()

    async def test_new_runner_refreshes_authentic_old_target_proof_but_refuses_tampering(self):
        await self.prepare();await self.ready()
        async with self.f.master.database.connect() as conn:
            publication=await conn.scalar(select(s.units.c.publication).where(s.units.c.unit_id==self.unit))
        proof=publication['target_live_reply']['proof'];old_epoch=proof['payload']['epoch']
        self.assertGreater(proof['payload']['expires_at'],self.f.now)
        await self.service.t.journal.release(self.lease)
        self.lease=await self.service.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        self.assertGreater(self.lease.epoch,old_epoch)
        damaged={**publication,'target_live_reply':{**publication['target_live_reply'],
            'proof':{**proof,'signature':p.encode(bytes(64))}}}
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(s.units).where(s.units.c.unit_id==self.unit).values(publication=damaged))
        with self.assertRaises(p.ProtocolError):await self.step()
        await self.retained();await self.target_preserved()
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(s.units).where(s.units.c.unit_id==self.unit).values(publication=publication))
        for _ in range(8):
            phase=(await self.step())['phase']
            await self.retained()
            if phase=='target_ready':break
            self.assertEqual(phase,'verifying_target')
        else:self.fail('New runner did not obtain a fresh Target proof')
        async with self.f.master.database.connect() as conn:
            fresh=await conn.scalar(select(s.units.c.publication).where(s.units.c.unit_id==self.unit))
        self.assertEqual(fresh['target_live_reply']['proof']['payload']['epoch'],self.lease.epoch)
        await self.finish();await self.target_preserved()

    async def test_lost_initial_native_intent_reply_keeps_master_runner_usable(self):
        await self.prepare()
        for _ in range(16):
            if (await self.step())['phase']=='target_ready':break
        else:self.fail('Target proof did not converge')
        original=self.lease
        async def lost(result):raise OSError('injected lost initial Source intent reply')
        self.after=lost
        with self.assertRaises(OSError):await self.step()
        self.assertEqual(self.lease,original)
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.tasks.c.state)),'committing')
        async with self.f.source.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.units.c.state)),'cleanup_pending')
        self.after=None;await self.finish();await self.target_preserved()

    async def test_expiry_during_unlink_has_no_success_ack_and_reconciles_the_durable_intent(self):
        await self.prepare();await self.ready();unlink=files.unlink
        def late(*args):
            result=unlink(*args);self.f.now+=46;return result
        with patch.object(files,'unlink',side_effect=late):
            with self.assertRaisesRegex(TransferError,'expired'):await self.step()
        self.assertEqual(sum(path.exists() for _,path,_ in self.resources),1)
        async with self.f.source.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.storage_journal.c.state)),'deleting')
        await self.finish();await self.target_preserved()

    async def test_boolean_cleanup_progress_is_refused_without_losing_the_real_task(self):
        await self.prepare();await self.ready()
        async def corrupt(result):result['deleted_count']=True
        self.after=corrupt
        with self.assertRaisesRegex(TransferError,'source_reply_invalid'):await self.step()
        self.after=None;await self.finish();await self.target_preserved()

    async def test_unknown_entry_can_be_reconciled_after_owner_preserves_it_elsewhere(self):
        await self.prepare();unknown=self.resources[0][1].parent/'owner-private.bin';unknown.write_bytes(b'keep owner data')
        with self.assertRaisesRegex(TransferError,'unknown_source_file'):await self.finish()
        # Explicit fixture-owner action, never automatic product cleanup.
        retained=self.f.root/'owner-retained.bin';unknown.rename(retained)
        await self.finish();await self.target_preserved();self.assertEqual(retained.read_bytes(),b'keep owner data')

    async def test_replaced_source_root_never_removes_new_host_data_or_old_originals(self):
        await self.prepare();await self.ready();root=self.f.source.roots['media'];moved=root.with_name('owner-original-root')
        root.rename(moved);root.mkdir();new=root/'owner-new-data.bin';new.write_bytes(b'keep new root')
        with self.assertRaisesRegex(TransferError,'root_changed'):await self.step()
        self.assertEqual(new.read_bytes(),b'keep new root')
        for _,path,data in self.resources:self.assertEqual((moved/path.relative_to(root)).read_bytes(),data)
        await self.target_preserved()

    async def test_recording_cleanup_preserves_target_footer_and_current_master_recordings(self):
        await self.fixture.fixture.test_recording_parent_rebind_preserves_opaque_footer_and_current_master_business()
        self.lease,self.unit=self.fixture.fixture.lease,self.fixture.fixture.unit_id
        self.resources=self.fixture.fixture.resources;self.kind='recording'
        await self.advance_clock(3601);await self.finish();await self.target_preserved()
        self.assertTrue(all(not path.exists() for _,path,_ in self.resources))

    async def test_source_identity_status_does_not_wait_for_the_physical_unlink(self):
        await self.prepare();await self.ready();entered,release=threading.Event(),threading.Event();unlink=files.unlink
        def held(*args):
            entered.set()
            if not release.wait(5):raise TimeoutError('fixture unlink was not released')
            return unlink(*args)
        async def status():
            async with self.f.source.database.begin() as conn:return await self.f.source.store.lock(conn)
        with patch.object(files,'unlink',side_effect=held):
            task=asyncio.create_task(self.step())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                identity=await asyncio.wait_for(status(),1)
                self.assertEqual(identity['node_id'],self.f.source.store.node['node_id']);self.assertFalse(task.done())
            finally:release.set();await task
        await self.finish();await self.target_preserved()
