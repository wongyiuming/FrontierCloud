"""Current Master/Source agreement preserves data and all release fences."""
import os
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete,func,select,text,update

from app.services import media_crypto,media_crypto_schema as cs,media_directories,resource_fences,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_catalog import CurrentCatalog,MARKER,digest_path
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_census as fixtures


@unittest.skipUnless(os.name=='posix','current catalog needs actual native source files')
class CurrentCancellationCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.CensusClientTests.asyncSetUp(self)
        self.lease=self.case.lease;self.catalog=self.new_catalog()

    def new_catalog(self):return CurrentCatalog(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
    asyncTearDown=fixtures.CensusClientTests.asyncTearDown
    preserved=fixtures.SourceCancellationCensusTests.preserved

    async def step(self):return await self.catalog.advance(self.lease)

    async def finish(self):
        for _ in range(60):
            result=await self.step()
            if result['master_catalog_verified']:return result
        self.fail('Current Master/Source catalog did not agree')

    async def usage(self):
        result=[]
        for peer in (self.f.master,self.f.source,self.f.target):
            async with peer.database.connect() as conn:
                result.append([tuple(row) for row in await conn.execute(select(fs.storage_members.c.member_id,
                    fs.storage_members.c.used_bytes,fs.storage_members.c.reserved_bytes).order_by(fs.storage_members.c.member_id))])
        return result

    async def test_current_catalog_agreement_reads_no_payload_and_releases_nothing(self):
        before=await self.usage()
        with (patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster')),
                patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted media')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read payload'))):
            result=await self.finish()
        self.assertEqual((result['retained_objects'],result['processed_objects']),(2,3))
        self.assertFalse(result['native_usage_reconciled']);self.assertFalse(result['source_drain_released']);self.assertFalse(result['task_completed'])
        self.assertEqual(before,await self.usage());await self.preserved()

    async def test_current_renamed_paths_match_without_rederiving_logical_ids(self):
        with patch.object(media_directories,'MEDIA_ROOT',self.f.source.roots['media']):
            await media_directories.rename_follower_directory(self.f.migration_retained_parent,'music/renamed',self.f.source.database)
        async with self.f.master.database.begin() as conn:
            for logical,path,_ in self.case.resources[1:]:
                current='music/renamed/'+path.name
                await conn.execute(update(fs.global_media).where(fs.global_media.c.media_id==logical).values(
                    media_path=current,path_locator=digest_path(current)))
        self.assertTrue((await self.finish())['master_catalog_verified'])
        for _,path,data in self.case.resources[1:]:self.assertEqual((path.parent.with_name('renamed')/path.name).read_bytes(),data)

    async def test_removed_catalog_and_native_registry_form_a_current_subset(self):
        logical,path,data=self.case.resources[1]
        saved=self.f.root/'fixture-owner-retained-original';path.rename(saved)
        async with self.f.master.database.begin() as conn:
            object_id=await conn.scalar(select(s.objects.c.source_object).where(s.objects.c.logical_id==logical))
            await conn.execute(delete(fs.global_media).where(fs.global_media.c.media_id==logical))
            await conn.execute(delete(cs.encryption).where(cs.encryption.c.object_id==logical))
        async with self.f.source.database.begin() as conn:
            await conn.execute(text('DELETE FROM media_objects WHERE media_id=:id'),dict(id=object_id))
            await conn.execute(delete(cs.encryption).where(cs.encryption.c.object_id==object_id))
        result=await self.finish();self.assertEqual(result['retained_objects'],1)
        self.assertEqual(saved.read_bytes(),data);self.assertFalse(result['source_drain_released'])

    async def test_same_count_and_bytes_with_different_master_path_cannot_agree(self):
        logical,_,_=self.case.resources[1];path=self.f.migration_retained_parent+'/mismatched.mp3'
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.global_media).where(fs.global_media.c.media_id==logical).values(
                media_path=path,path_locator=digest_path(path)))
        with self.assertRaisesRegex(TransferError,'source_mismatch'):await self.finish()
        await self.preserved()

    async def test_deleted_encrypted_objects_keep_retired_key_records_and_agree(self):
        logical,path,data=self.case.resources[2];saved=self.f.root/'fixture-owner-deleted-ciphertext';path.rename(saved)
        async with self.f.master.database.begin() as conn:
            object_id=await conn.scalar(select(s.objects.c.source_object).where(s.objects.c.logical_id==logical))
            master_file_id=await conn.scalar(select(cs.encryption.c.file_id).where(cs.encryption.c.object_id==logical))
            await media_crypto.forget_encryption(conn,logical)
            await conn.execute(delete(fs.global_media).where(fs.global_media.c.media_id==logical))
        async with self.f.source.database.begin() as conn:
            source_file_id=await conn.scalar(select(cs.encryption.c.file_id).where(cs.encryption.c.object_id==object_id))
            await media_crypto.forget_encryption(conn,object_id)
            await conn.execute(text('DELETE FROM media_objects WHERE media_id=:id'),dict(id=object_id))
        self.assertEqual((await self.finish())['retained_objects'],1);self.assertEqual(saved.read_bytes(),data)
        for peer,key,file_id in ((self.f.master,logical,master_file_id),(self.f.source,object_id,source_file_id)):
            async with peer.database.connect() as conn:
                row=(await conn.execute(select(cs.encryption).where(cs.encryption.c.object_id==key))).mappings().one()
                self.assertEqual(row['file_id'],file_id);self.assertIsNone(row['descriptor_json'])

    async def test_missing_master_catalog_with_live_descriptor_still_refuses(self):
        logical,_,_=self.case.resources[2]
        async with self.f.master.database.begin() as conn:
            await conn.execute(delete(fs.global_media).where(fs.global_media.c.media_id==logical))
        with self.assertRaisesRegex(TransferError,'orphan_encryption'):await self.finish()
        await self.preserved()

    async def test_master_fence_precedes_native_census_and_spares_unrelated_paths(self):
        await self.step();self.assertEqual(self.calls,[])
        async with self.f.master.database.begin() as conn:
            with self.assertRaises(resource_fences.FenceConflict):await resource_fences.paths(conn,'media',[(self.f.migration_retained_parent+'/plain.mp3',False)])
            with self.assertRaises(resource_fences.FenceConflict):await resource_fences.paths(conn,'media',[('music',True)])
            await resource_fences.paths(conn,'media',[('music/unrelated',True)])
            await resource_fences.paths(conn,'media',[('music/retained-two/file.mp3',False)])
        await self.finish();await self.preserved()

    async def test_damaged_marker_retains_fence_and_refuses_restart(self):
        await self.step()
        async with self.f.master.database.begin() as conn:
            plan=await conn.scalar(select(s.tasks.c.plan))
            await conn.execute(update(s.tasks).values(plan={**plan,MARKER:None}))
        self.catalog=self.new_catalog()
        with self.assertRaisesRegex(TransferError,'progress_changed'):await self.step()
        async with self.f.master.database.begin() as conn:
            with self.assertRaises(resource_fences.FenceConflict):await resource_fences.paths(conn,'media',[(self.f.migration_retained_parent,True)])
        await self.preserved()

    async def test_process_restart_and_new_runner_recount_without_releasing_fence(self):
        await self.step();self.catalog=self.new_catalog();await self.step()
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.runners).values(expires_at=0))
        self.lease=await self.catalog.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        self.assertTrue((await self.finish())['master_catalog_verified']);await self.preserved()

    async def test_audit_failure_rolls_back_initial_marker_without_contacting_source(self):
        with patch.object(self.f.master.store,'log',side_effect=RuntimeError('fixture audit unavailable')):
            with self.assertRaisesRegex(RuntimeError,'audit unavailable'):await self.step()
        async with self.f.master.database.connect() as conn:self.assertNotIn(MARKER,await conn.scalar(select(s.tasks.c.plan)))
        self.assertEqual(self.calls,[]);await self.finish();await self.preserved()


@unittest.skipUnless(os.name=='posix','recording agreement needs native files')
class CurrentRecordingCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.SourceRecordingCensusTests.asyncSetUp(self)
        self.lease=self.case.lease
        self.catalog=CurrentCatalog(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        original=self.f.transport.request
        async def request(origin,path,**kwargs):
            if not path.endswith('/cancel-source-census'):return await original(origin,path,**kwargs)
            self.assertEqual((origin,kwargs['credential'],kwargs['relation']),
                (self.f.source.store.node['endpoint'],self.f.source.credential,self.f.source.relationship))
            result=await self.service.advance(self.f.source.store.node,self.relation,**kwargs['value'])
            return p.sign(self.f.source.store.unseal(self.f.source.store.node['private_key']),result)
        self.f.transport.request=request

    asyncTearDown=fixtures.SourceRecordingCensusTests.asyncTearDown
    step=CurrentCancellationCatalogTests.step
    finish=CurrentCancellationCatalogTests.finish

    async def test_recording_ids_and_opaque_footers_agree_without_payload_reads(self):
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted recording')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read footer'))):
            result=await self.finish()
        self.assertEqual(result['retained_objects'],len(self.resources));self.assertFalse(result['task_completed'])
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)
        async with self.f.master.database.begin() as conn:
            with self.assertRaises(resource_fences.FenceConflict):
                await resource_fences.recording(conn,self.f.source.store.node['node_id'],self.resources[0][1].parent.name)
            await resource_fences.recording(conn,self.f.target.store.node['node_id'],self.resources[0][1].parent.name)

    async def test_missing_native_recording_cannot_claim_agreement_with_master(self):
        _,path,data=self.resources[0];saved=self.f.root/'fixture-owner-recording';path.rename(saved)
        with self.assertRaisesRegex(TransferError,'source_mismatch'):await self.finish()
        self.assertEqual(saved.read_bytes(),data)
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.tasks.c.state)),'cancelling')
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.drains)),1)


@unittest.skipUnless(os.name=='posix','encrypted cleanup needs native files')
class CommittedEncryptedCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        case_class=fixtures.recording_fixtures.MasterCancellationVariantsTests
        original=case_class.mixed
        async def encrypted(case):return await original(case,committed_encrypted=True)
        with patch.object(case_class,'mixed',encrypted):await CurrentCancellationCatalogTests.asyncSetUp(self)

    asyncTearDown=CurrentCancellationCatalogTests.asyncTearDown
    new_catalog=CurrentCancellationCatalogTests.new_catalog
    step=CurrentCancellationCatalogTests.step
    finish=CurrentCancellationCatalogTests.finish
    preserved=CurrentCancellationCatalogTests.preserved

    async def test_committed_encrypted_cleanup_keeps_native_tombstone_and_allows_current_agreement(self):
        logical,path,_=self.case.resources[0]
        async with self.f.master.database.connect() as conn:
            object_id=await conn.scalar(select(s.objects.c.source_object).where(s.objects.c.logical_id==logical))
        self.assertFalse(path.exists())
        async with self.f.source.database.connect() as conn:
            row=(await conn.execute(select(cs.encryption).where(cs.encryption.c.object_id==object_id))).mappings().one()
            self.assertIsNone(row['descriptor_json']);file_id=row['file_id']
        self.assertTrue((await self.finish())['master_catalog_verified']);await self.preserved()
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(cs.encryption.c.file_id).where(cs.encryption.c.object_id==object_id)),file_id)


if __name__=='__main__':unittest.main()
