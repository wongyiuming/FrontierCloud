"""Native current-census boundaries; neither terminal cancellation nor TLS proof."""
import asyncio
import json
import os
import threading
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import func,select,text,update

from app.services import media_crypto,media_directories,resource_cancel_census as census,resource_cancel_census_schema as c
from app.services import resource_fences,resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_cancel_census_client import CensusClient
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_cleanup as fixtures
from tests import test_resource_cancel_master as recording_fixtures
from app.services.federation import protocol as p


@unittest.skipUnless(os.name=='posix','native current census requires Linux nofollow I/O')
class SourceCancellationCensusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case=fixtures.MixedCancellationCleanupTests('runTest');await self.case.asyncSetUp()
        await self.case.clock(3601);await self.case.finish()
        self.f=self.case.f;self.service=census.SourceCensus(self.f.source.transfers)
        async with self.f.source.database.begin() as conn:
            self.relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            await conn.execute(update(fs.storage_members).values(used_bytes=512*1024))
        self.used=512*1024

    async def asyncTearDown(self):await self.case.asyncTearDown()

    async def grant(self):
        async with self.f.master.database.begin() as conn:task=await self.case.service.t.journal._fenced(conn,self.case.lease)
        relations=await self.case.service.t.relations(task)
        return self.case.service.t._grant(task,relations,self.case.lease.epoch,self.case.lease.runner_id,'finalize')

    async def step(self):
        return await self.service.advance(self.f.source.store.node,self.relation,uuid.uuid4().hex,await self.grant())

    async def reach(self,phase):
        for _ in range(40):
            value=await self.step()
            if value['phase']==phase:return value
        self.fail('Source current census did not reach '+phase)

    async def preserved(self):
        await self.case.retained()
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),self.used)
            self.assertEqual(await conn.scalar(select(s.peer_reservations.c.state)),'active')
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.drains)),1)
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.tasks.c.state)),'cancelling')

    async def test_bounded_metadata_census_preserves_ciphertext_usage_drain_and_master_task(self):
        with (patch.object(media_crypto,'open_premaster',side_effect=AssertionError('Opened media premaster')),
                patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('Decrypted media')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('Read media payload'))):
            value=await self.reach('verified')
        proof=census.verify_proof(value['proof'],(await self.grant())['payload'],self.f.now)
        self.assertEqual((proof['object_count'],proof['total_bytes']),(2,sum(len(data) for _,_,data in self.case.resources[1:])))
        self.assertFalse(value['native_usage_reconciled']);self.assertFalse(value['source_drain_released']);self.assertFalse(value['task_completed'])
        async with self.f.source.database.connect() as conn:
            rows=list((await conn.execute(select(c.snapshots))).mappings());self.assertEqual(len(rows),2)
            self.assertEqual({row['seen'] for row in rows},{1})
        await self.preserved()


    async def test_native_managed_rename_before_census_uses_current_paths_and_stable_ids(self):
        with patch.object(media_directories,'MEDIA_ROOT',self.f.source.roots['media']):
            await media_directories.rename_follower_directory(self.f.migration_retained_parent,'music/renamed',self.f.source.database)
        result=await self.reach('verified');self.assertEqual(result['retained_objects'],2)
        async with self.f.source.database.connect() as conn:
            rows=list((await conn.execute(select(c.snapshots))).mappings())
            self.assertEqual({row['logical_id'] for row in rows},{logical for logical,_,_ in self.case.resources[1:]})
            self.assertTrue(all(row['physical_path'].startswith('music/renamed/') for row in rows))
        for _,path,data in self.case.resources[1:]:self.assertEqual((path.parent.with_name('renamed')/path.name).read_bytes(),data)

    async def test_census_fence_blocks_managed_rename_and_recording_mutations(self):
        await self.step()
        with patch.object(media_directories,'MEDIA_ROOT',self.f.source.roots['media']):
            with self.assertRaises(resource_fences.FenceConflict):
                await media_directories.rename_follower_directory(self.f.migration_retained_parent,'music/renamed',self.f.source.database)
        async with self.f.source.database.begin() as conn:
            with self.assertRaises(resource_fences.FenceConflict):await resource_fences.recording(conn,self.f.source.store.node['node_id'],'a'*32)
        await self.reach('verified');await self.preserved()

    async def test_unknown_file_is_preserved_and_cannot_become_usage_or_a_release_proof(self):
        unknown=self.f.source.roots['media']/'owner-file.bin';unknown.write_bytes(b'Owner data')
        with self.assertRaisesRegex(TransferError,'unknown_file'):await self.reach('verified')
        self.assertEqual(unknown.read_bytes(),b'Owner data');await self.preserved()

    async def test_live_orphan_descriptor_still_refuses_after_native_registry_disappears(self):
        logical,path,data=self.case.resources[2];saved=self.f.root/'fixture-owner-orphan-ciphertext';path.rename(saved)
        async with self.f.master.database.connect() as conn:
            object_id=await conn.scalar(select(s.objects.c.source_object).where(s.objects.c.logical_id==logical))
        async with self.f.source.database.begin() as conn:
            await conn.execute(text('DELETE FROM media_objects WHERE media_id=:id'),dict(id=object_id))
        with self.assertRaisesRegex(TransferError,'orphan_encryption'):await self.reach('verified')
        self.assertEqual(saved.read_bytes(),data)

    async def test_registered_missing_file_refuses_without_discarding_its_registry(self):
        _,path,_=self.case.resources[1];path.unlink()
        with self.assertRaisesRegex(TransferError,'registered_file_missing'):await self.reach('verified')
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM media_objects')),2)
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),self.used)

    async def test_same_length_external_rewrite_after_roster_invalidates_physical_census(self):
        await self.reach('physical');_,path,data=self.case.resources[1];path.write_bytes(b'x'*len(data))
        with self.assertRaisesRegex(TransferError,'file_changed'):await self.reach('verified')
        self.assertEqual(path.read_bytes(),b'x'*len(data))
        async with self.f.source.database.connect() as conn:self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),self.used)

    async def test_fresh_process_restarts_census_without_releasing_the_durable_fence(self):
        await self.reach('physical')
        async with self.f.source.database.connect() as conn:first=(await conn.scalar(select(s.peer_reservations.c.payload)))['transfer']['cancel_census']['census_id']
        await self.f.source.transfers.close()
        replacement=TransferPeers(self.f.source.store,root_provider=lambda:self.f.source.roots,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.case.extra.append(replacement);self.f.source.transfers=replacement;self.service=census.SourceCensus(replacement)
        self.assertEqual((await self.step())['phase'],'roster');value=await self.reach('verified')
        self.assertNotEqual(first,value['proof']['payload']['census_id']);await self.preserved()

    async def test_lost_verified_reply_replays_same_proof_until_its_original_expiry(self):
        first=await self.reach('verified');second=await self.step();self.assertEqual(first['proof'],second['proof'])
        self.f.now+=46;value=await self.step();self.assertEqual(value['phase'],'roster');self.assertIsNone(value['proof'])
        await self.reach('verified');await self.preserved()

    async def test_null_journal_is_not_overwritten_and_still_fences_writes(self):
        await self.step()
        async with self.f.source.database.begin() as conn:
            payload=await conn.scalar(select(s.peer_reservations.c.payload))
            await conn.execute(update(s.peer_reservations).values(payload={**payload,'transfer':{**payload['transfer'],'cancel_census':None}}))
        with self.assertRaisesRegex(TransferError,'progress_changed'):await self.step()
        async with self.f.source.database.begin() as conn:
            with self.assertRaises(resource_fences.FenceConflict):await resource_fences.paths(conn,'media',[(self.f.migration_retained_parent,True)])
        await self.preserved()

    async def test_unsettled_native_unit_refuses_before_installing_the_new_fence(self):
        async with self.f.source.database.begin() as conn:
            await conn.execute(update(s.units).where(s.units.c.state=='cancelled').values(native_acknowledged=0))
        with self.assertRaisesRegex(TransferError,'terminal_units_required'):await self.step()
        async with self.f.source.database.connect() as conn:
            self.assertNotIn('cancel_census',(await conn.scalar(select(s.peer_reservations.c.payload)))['transfer'])
        await self.preserved()

    async def test_forged_verified_sql_state_cannot_replace_physical_evidence(self):
        await self.step()
        async with self.f.source.database.begin() as conn:
            payload=await conn.scalar(select(s.peer_reservations.c.payload));progress=payload['transfer']['cancel_census']
            await conn.execute(update(s.peer_reservations).values(payload={**payload,'transfer':{**payload['transfer'],
                'cancel_census':{**progress,'phase':'verified'}}}))
        with self.assertRaisesRegex(TransferError,'physical_proof_required'):await self.step()
        await self.preserved()

    async def test_audit_failure_rolls_back_snapshot_page_and_retry_keeps_source_data(self):
        await self.step();original=self.f.source.store.log
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_cancel_census_step':raise OSError('Controlled census audit failure')
            return await original(conn,action,*args,**kwargs)
        with patch.object(self.f.source.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.reach('physical')
        await self.reach('verified');await self.preserved()

    async def test_active_reader_does_not_prevent_nondeleting_metadata_census(self):
        self.f.traffic.entered()
        try:await self.reach('verified')
        finally:self.f.traffic.exited()
        await self.preserved()

    async def test_damaged_signed_census_is_refused_without_renewing_its_proof(self):
        await self.reach('verified')
        async with self.f.source.database.begin() as conn:
            payload=await conn.scalar(select(s.peer_reservations.c.payload));progress=payload['transfer']['cancel_census']
            proof=progress['proof'];bad={**proof,'payload':{**proof['payload'],'total_bytes':0}}
            await conn.execute(update(s.peer_reservations).values(payload={**payload,'transfer':{**payload['transfer'],
                'cancel_census':{**progress,'proof':bad}}}))
        with self.assertRaisesRegex(TransferError,'proof_invalid'):await self.step()
        await self.preserved()

    async def test_source_identity_transaction_remains_available_while_stat_is_blocked(self):
        await self.step();entered=threading.Event();release=threading.Event();original=census.current_file
        def held(*args):entered.set();release.wait(5);return original(*args)
        with patch.object(census,'current_file',side_effect=held):
            task=asyncio.create_task(self.reach('physical'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,5))
                async with asyncio.timeout(1):
                    async with self.f.source.database.begin() as conn:await self.f.source.store.lock(conn)
            finally:release.set();await task
        await self.reach('verified');await self.preserved()

    async def test_inactive_scan_reaper_closes_descriptors_and_keeps_fence(self):
        for index in range(110):(self.f.source.roots['media']/('empty-'+str(index))).mkdir()
        await self.reach('physical');await self.step();scan=self.f.source.transfers.source_cancel_census
        self.assertIsNotNone(scan.walker)
        self.f.now+=301;task=asyncio.create_task(self.f.source.transfers.reap_cancel_census())
        try:
            async with asyncio.timeout(3):
                while self.f.source.transfers.source_cancel_census is not None:await asyncio.sleep(.05)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertIsNone(scan.walker)
        async with self.f.source.database.begin() as conn:
            with self.assertRaises(resource_fences.FenceConflict):await resource_fences.paths(conn,'media',[(self.f.migration_retained_parent,True)])
        await self.preserved()


    async def test_damaged_snapshot_catalog_cannot_become_a_physical_proof(self):
        await self.reach('physical')
        async with self.f.source.database.begin() as conn:await conn.execute(update(c.snapshots).values(catalog_json='{}'))
        with self.assertRaisesRegex(TransferError,'metadata_changed'):await self.reach('verified')
        await self.preserved()


@unittest.skipUnless(os.name=='posix','recording census requires native Linux files')
class SourceRecordingCensusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case=recording_fixtures.MasterCancellationVariantsTests('runTest');await self.case.asyncSetUp()
        await self.case.base.prepare_recordings();await self.case.cancel(self.case.base.lease);await self.case.finish()
        self.f=self.case.f;self.service=census.SourceCensus(self.f.source.transfers);self.resources=self.case.base.resources
        async with self.f.source.database.begin() as conn:
            self.relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            await conn.execute(update(fs.storage_members).values(used_bytes=512*1024))

    async def asyncTearDown(self):await self.case.asyncTearDown()
    grant=SourceCancellationCensusTests.grant
    step=SourceCancellationCensusTests.step
    reach=SourceCancellationCensusTests.reach

    async def test_opaque_recording_footers_and_thirty_two_character_ids_are_preserved(self):
        with (patch.object(media_crypto,'open_premaster',side_effect=AssertionError('Opened media premaster')),
                patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('Decrypted recording')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('Read recording payload'))):
            result=await self.reach('verified')
        proof=census.verify_proof(result['proof'],(await self.grant())['payload'],self.f.now)
        self.assertEqual((proof['object_count'],proof['total_bytes']),(len(self.resources),sum(len(data) for _,_,data in self.resources)))
        async with self.f.source.database.connect() as conn:
            rows=list((await conn.execute(select(c.snapshots))).mappings())
            self.assertEqual({row['logical_id'] for row in rows},{logical for logical,_,_ in self.resources})
            self.assertEqual({row['resource_kind'] for row in rows},{'recording'})
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),512*1024)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.drains)),1)
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)

    async def test_absent_native_recording_reports_a_subset_without_claiming_master_agreement(self):
        _,path,data=self.resources[0];retained=self.f.root/'fixture-owner-retained-recording.bin';path.rename(retained)
        result=await self.reach('verified')
        self.assertEqual((result['retained_objects'],result['retained_bytes']),(len(self.resources)-1,sum(len(item[2]) for item in self.resources[1:])))
        self.assertFalse(result['task_completed']);self.assertFalse(result['native_usage_reconciled'])
        self.assertEqual(retained.read_bytes(),data)

    async def test_unknown_recording_file_is_preserved_and_refuses_a_census_proof(self):
        unknown=self.resources[0][1].with_name('f'*32+'.bin');unknown.write_bytes(b'Unknown owner recording')
        with self.assertRaisesRegex(TransferError,'unknown_file'):await self.reach('verified')
        self.assertEqual(unknown.read_bytes(),b'Unknown owner recording')


@unittest.skipUnless(os.name=='posix','typed census controls require native fixtures')
class CensusClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await SourceCancellationCensusTests.asyncSetUp(self)
        self.client=CensusClient(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.calls=[];self.mutate=None;original=self.f.transport.request
        async def request(origin,path,**kwargs):
            if not path.endswith('/cancel-source-census'):return await original(origin,path,**kwargs)
            self.assertEqual((origin,kwargs['credential'],kwargs['relation']),
                (self.f.source.store.node['endpoint'],self.f.source.credential,self.f.source.relationship))
            self.calls.append(kwargs['value'])
            result=await self.service.advance(self.f.source.store.node,self.relation,**kwargs['value'])
            if self.mutate:result=self.mutate(result)
            return p.sign(self.f.source.store.unseal(self.f.source.store.node['private_key']),result)
        self.f.transport.request=request

    asyncTearDown=SourceCancellationCensusTests.asyncTearDown
    preserved=SourceCancellationCensusTests.preserved
    reach=SourceCancellationCensusTests.reach
    async def step(self):return await self.client.query(self.case.lease)

    async def test_typed_source_proof_does_not_claim_master_catalog_or_task_completion(self):
        value=await self.reach('verified');self.assertFalse(value['master_catalog_verified'])
        self.assertEqual(value['retained_objects'],2);self.assertGreater(len(self.calls),1);await self.preserved()

    async def test_validly_signed_boolean_counter_and_damaged_nested_proof_refuse(self):
        self.mutate=lambda value:{**value,'processed_objects':True}
        with self.assertRaisesRegex(TransferError,'peer_reply_invalid'):await self.step()
        def corrupt(value):
            if value['proof'] is None:return value
            return {**value,'proof':{**value['proof'],'payload':{**value['proof']['payload'],'total_bytes':0}}}
        self.mutate=corrupt
        with self.assertRaisesRegex(TransferError,'proof_invalid'):await self.reach('verified')
        self.mutate=None;await self.reach('verified');await self.preserved()

    async def test_incomplete_master_settlement_refuses_before_installing_native_census_fence(self):
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(s.units).where(s.units.c.state=='cancelled').values(native_acknowledged=0))
        with self.assertRaisesRegex(TransferError,'master_units_incomplete'):await self.step()
        self.assertEqual(self.calls,[])
        async with self.f.source.database.connect() as conn:
            self.assertNotIn('cancel_census',(await conn.scalar(select(s.peer_reservations.c.payload)))['transfer'])
        await self.preserved()
