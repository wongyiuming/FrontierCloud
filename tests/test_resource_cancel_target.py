"""Real native rollback of verified Target shadows with Source signatures.

Public post-publication Master cancellation is not enabled by this fixture.
"""
import copy
import asyncio
import os
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import select,text

from app.services import media_crypto,media_crypto_schema as crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_target import TargetCancellation,selected,verify_receipt
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_source as fixtures


@unittest.skipUnless(os.name=='posix','Target rollback uses actual native Linux inodes')
class CancellationTargetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.CancellationSourceTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f;self.lease=self.fixture.lease;self.unit=self.fixture.fixture.unit
        self.service=TargetCancellation(self.f.target.transfers)
        self.extra_services=[]
        async with self.f.target.database.connect() as conn:
            self.relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
        self.proof=await self.fixture.finish()

    async def asyncTearDown(self):
        for service in self.extra_services:await service.close()
        await self.fixture.asyncTearDown()

    async def grant(self):
        async with self.f.master.database.begin() as conn:task=await self.fixture.service.transfer.journal._fenced(conn,self.lease)
        relations=await self.fixture.service.transfer.relations(task)
        return self.fixture.service.transfer._grant(task,relations,self.lease.epoch,self.lease.runner_id,'cancel')

    async def step(self,*,proof=True):
        self.last_grant=await self.grant()
        return await self.service.advance(self.f.target.store.node,self.relation,uuid.uuid4().hex,self.last_grant,self.unit,self.proof if proof else None)

    async def finish(self):
        for _ in range(20):
            result=await self.step()
            if result['phase']=='cancelled':return result
        self.fail('Native Target unit rollback did not converge')

    async def domain(self):
        result={}
        for name,peer in (('master',self.f.master),('source',self.f.source),('target',self.f.target)):
            async with peer.database.connect() as conn:
                tables=(fs.identity,fs.storage_members,s.capacity,s.scopes,s.drains)
                if name!='target':tables=(*tables,fs.global_media,crypto.encryption)
                result[name]={table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in tables}
        return copy.deepcopy(result)

    async def originals(self):
        for _,path,data in self.fixture.fixture.fixture.resources:self.assertEqual(path.read_bytes(),data)

    async def test_plain_and_encrypted_publication_rolls_back_without_quota_refund_or_source_changes(self):
        before=await self.domain()
        async with self.f.target.database.connect() as conn:
            encrypted=[dict(row) for row in (await conn.execute(select(crypto.encryption))).mappings()]
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('media key opened')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('premaster opened'))):
            result=await self.finish()
        self.assertFalse(result['capacity_settled']);self.assertFalse(result['source_files_removed']);self.assertEqual(result['deleted_count'],2)
        self.assertEqual(await self.domain(),before);await self.originals()
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM media_objects')),0)
            after=[dict(row) for row in (await conn.execute(select(crypto.encryption))).mappings()]
            self.assertEqual(after,[{**row,'descriptor_json':None} for row in encrypted])
            unit=dict((await conn.execute(select(s.units))).mappings().one())
            self.assertEqual(unit['state'],'cancelled')
        chosen=selected(unit,self.last_grant['payload'])
        verify_receipt(result['receipt'],self.last_grant['payload']['target_key'],self.relation['peer_key'],self.last_grant['payload'],chosen,self.f.now)

    async def test_missing_invalid_or_expired_source_proof_cannot_remove_shadows(self):
        before=await self.domain();original=self.proof
        with self.assertRaisesRegex(TransferError,'source_proof_invalid'):await self.step(proof=False)
        self.proof={**original,'source_reply':p.sign(self.f.master.store.unseal(self.f.master.store.node['private_key']),original['source_reply']['payload'])}
        with self.assertRaisesRegex(TransferError,'source_proof_invalid'):await self.step()
        self.proof=original;grant=await self.grant();self.f.now+=46
        refreshed=p.sign(self.f.master.store.unseal(self.f.master.store.node['private_key']),
            {**grant['payload'],'created_at':int(self.f.now),'expires_at':int(self.f.now)+45,'nonce':uuid.uuid4().hex})
        with self.assertRaisesRegex(TransferError,'authorization_expired'):
            await self.service.advance(self.f.target.store.node,self.relation,uuid.uuid4().hex,refreshed,self.unit,self.proof)
        self.assertEqual(await self.domain(),before);await self.fixture.fixture.originals()

    async def test_unknown_public_sibling_is_retained_and_no_terminal_receipt_or_quota_release_is_created(self):
        first=self.fixture.fixture.fixture.resources[0][1]
        target=self.f.target.roots['media']/first.relative_to(self.f.source.roots['media'])
        unknown=target.with_name('unknown.bin');unknown.write_bytes(b'keep unknown external material')
        before=await self.domain()
        with self.assertRaisesRegex(TransferError,'unknown_publication_entry'):await self.finish()
        self.assertEqual(unknown.read_bytes(),b'keep unknown external material');self.assertEqual(await self.domain(),before);await self.originals()
        async with self.f.target.database.connect() as conn:
            unit=dict((await conn.execute(select(s.units))).mappings().one())
            self.assertNotEqual(unit['state'],'cancelled');self.assertNotIn('receipt',unit['publication']['target_cancel'])

    async def test_lost_unlink_reply_reconciles_only_persisted_intent_with_a_new_transfer_instance(self):
        from app.services import resource_cancel_files as files
        original=files.unlink_object;lost=False
        def unlink(*args):
            nonlocal lost
            original(*args)
            if not lost:lost=True;raise OSError('controlled lost unlink reply')
        before=await self.domain()
        with patch.object(files,'unlink_object',side_effect=unlink):
            with self.assertRaises(OSError):await self.step()
        self.assertTrue(lost);self.assertEqual(await self.domain(),before)
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.storage_journal.c.state).where(s.storage_journal.c.state=='aborting')),'aborting')
        await self.f.target.transfers.close()
        self.f.target.transfers=TransferPeers(self.f.target.store,root_provider=lambda:self.f.target.roots,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.extra_services.append(self.f.target.transfers);self.service=TargetCancellation(self.f.target.transfers)
        result=await self.finish();self.assertEqual(result['deleted_count'],2);self.assertEqual(await self.domain(),before);await self.originals()

    async def test_existing_read_lease_waits_and_new_reads_refuse_after_durable_cancel_intent(self):
        from app.services import media_manager,resource_readers
        from fastapi import HTTPException
        logical,path,_=self.fixture.fixture.fixture.resources[0]
        relative=path.relative_to(self.f.source.roots['media']).as_posix()
        async with self.f.target.database.connect() as conn:
            object_id=await conn.scalar(text('SELECT media_id FROM media_objects WHERE media_path=:path'),dict(path=relative))
        with patch.object(media_manager,'MEDIA_ROOT',self.f.target.roots['media']):
            lease=await resource_readers.media(self.f.target.store,self.relation,object_id,logical,relative,int(time.time())+30)
        result=await self.step();self.assertEqual(result['phase'],'waiting_reader');self.assertEqual(result['deleted_count'],0)
        with patch.object(media_manager,'MEDIA_ROOT',self.f.target.roots['media']):
            with self.assertRaises(HTTPException) as refused:
                await resource_readers.media(self.f.target.store,self.relation,object_id,logical,relative,int(time.time())+30)
        self.assertEqual(refused.exception.status_code,409)
        from sqlalchemy import delete
        async with self.f.target.database.begin() as conn:await conn.execute(delete(s.read_leases).where(s.read_leases.c.lease_id==lease))
        await self.finish();await self.originals()

    async def test_actual_asgi_hmac_control_refuses_nonce_replay_and_returns_signed_unit_receipt(self):
        import httpx
        from fastapi import FastAPI
        from app.api import internal_nodes,internal_resource_inventory,internal_resource_transfer as api
        application=FastAPI();application.include_router(api.router)
        path='/internal/v1/migration/transfer/cancel-target'
        key=p.public_key(self.f.target.store.unseal(self.f.target.store.node['private_key']))
        with (patch.object(internal_nodes,'state',self.f.target.store),patch.object(internal_resource_inventory,'state',self.f.target.store),
                patch.object(api,'transfers',self.f.target.transfers)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url=self.f.target.store.node['endpoint']) as client:
                for index in range(20):
                    grant=await self.grant();value=dict(request_id=uuid.uuid4().hex,grant=grant,unit_id=self.unit,proof=self.proof)
                    raw=p.canonical(value);headers=p.auth_headers(self.f.target.credential,self.f.target.relationship,'POST',path,raw)
                    response=await client.post(path,content=raw,headers=headers);self.assertEqual(response.status_code,200,response.text)
                    result=p.verify(key,response.json());self.assertEqual(result['request_id'],value['request_id'])
                    if index==0:self.assertEqual((await client.post(path,content=raw,headers=headers)).status_code,401)
                    if result['phase']=='cancelled':break
                else:self.fail('Authenticated ASGI cancellation did not finish')
                self.assertIsNotNone(result['receipt']);self.assertEqual(result['deleted_count'],2)
        await self.originals()

    async def test_terminal_replay_after_expiry_and_new_epoch_keeps_later_external_material(self):
        from sqlalchemy import update
        result=await self.finish();before=await self.domain();self.f.now+=100
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.relationships).values(last_heartbeat=int(self.f.now)))
        self.lease=await self.fixture.service.transfer.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        source=self.fixture.fixture.fixture.resources[0][1]
        foreign=self.f.target.roots['media']/source.relative_to(self.f.source.roots['media'])
        foreign.parent.mkdir(parents=True);foreign.write_bytes(b'later external material is not another unlink permission')
        replay=await self.step(proof=False)
        self.assertEqual(replay['receipt'],result['receipt']);self.assertEqual(await self.domain(),before)
        self.assertEqual(foreign.read_bytes(),b'later external material is not another unlink permission');await self.originals()

    async def test_post_unlink_audit_rollback_keeps_metadata_and_durable_intent_for_retry(self):
        before=await self.domain();log=self.f.target.store.log
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_target_shadow_removed':raise RuntimeError('controlled audit rollback after unlink')
            return await log(conn,action,*args,**kwargs)
        with patch.object(self.f.target.store,'log',side_effect=fail):
            with self.assertRaises(RuntimeError):await self.step()
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM media_objects')),2)
            self.assertEqual(await conn.scalar(select(s.storage_journal.c.state).where(s.storage_journal.c.state=='aborting')),'aborting')
        self.assertEqual(await self.domain(),before);result=await self.finish()
        self.assertEqual(result['deleted_count'],2);self.assertEqual(await self.domain(),before);await self.originals()

    async def test_terminal_replay_requires_complete_typed_progress_and_final_source_authority(self):
        from sqlalchemy import update
        result=await self.finish();before=await self.domain()
        async with self.f.target.database.connect() as conn:
            original=copy.deepcopy(await conn.scalar(select(s.units.c.publication)))
        source=self.fixture.fixture.fixture.resources[0][1]
        later=self.f.target.roots['media']/source.relative_to(self.f.source.roots['media'])
        later.parent.mkdir(parents=True);later.write_bytes(b'preserve later external material')
        progress=original['target_cancel']
        cases=[None,[],dict(progress,final_proof=None),dict(progress,final_proof={}),
            {name:value for name,value in progress.items() if name!='final_proof'},dict(progress,version=True),
            dict(progress,receipt_count=True),dict(progress,receipt_count=0),dict(progress,receipt_roster='0'*64),
            dict(progress,step='objects')]
        for changed in cases:
            with self.subTest(progress=changed):
                async with self.f.target.database.begin() as conn:
                    await conn.execute(update(s.units).values(publication={**original,'target_cancel':changed}))
                with self.assertRaisesRegex(TransferError,'cancel_journal_changed'):await self.step(proof=False)
                self.assertEqual(later.read_bytes(),b'preserve later external material');self.assertEqual(await self.domain(),before)
        async with self.f.target.database.begin() as conn:await conn.execute(update(s.units).values(publication=original))
        replay=await self.step(proof=False);self.assertEqual(replay['receipt'],result['receipt']);await self.originals()

    async def test_actual_native_commit_refuses_an_older_valid_cancellation_permission(self):
        from sqlalchemy import update
        from app.services.resource_commit_peers import CommitPeers
        from app.services.resource_cutover_master import Cutover
        async with self.f.master.database.begin() as conn:
            revision=await conn.scalar(select(s.tasks.c.revision))
            await conn.execute(update(s.tasks).values(state='committing',revision=revision+1));await conn.execute(update(s.runners).values(expires_at=0))
        cutover=Cutover(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.lease=await cutover.transfer.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        for _ in range(24):
            if (await cutover.publication.target_step(self.lease,self.unit))['target_published']:break
        else:self.fail('Fresh target proof did not converge')
        for _ in range(24):
            if (await cutover.publication.source_step(self.lease,self.unit))['source_verified']:break
        else:self.fail('Fresh source proof did not converge')
        self.lease,committed=await cutover.commit(self.lease,self.unit)
        async with self.f.master.database.begin() as conn:task=await cutover.transfer.journal._fenced(conn,self.lease)
        relations=await cutover.transfer.relations(task)
        commit_grant=cutover.transfer._grant(task,relations,self.lease.epoch,self.lease.runner_id,'commit')
        await CommitPeers(self.f.target.transfers).acknowledge(self.f.target.store.node,self.relation,uuid.uuid4().hex,commit_grant,committed['commit'])
        before=await self.domain();cancel_grant=cutover.transfer._grant(task,relations,self.lease.epoch,self.lease.runner_id,'cancel')
        with self.assertRaisesRegex(TransferError,'native_commit_is_final'):
            await self.service.advance(self.f.target.store.node,self.relation,uuid.uuid4().hex,cancel_grant,self.unit,self.proof)
        self.assertEqual(await self.domain(),before);await self.fixture.fixture.originals()

    async def test_registry_change_refuses_before_any_owned_unlink(self):
        async with self.f.target.database.begin() as conn:
            object_id=await conn.scalar(select(s.storage_journal.c.object_id).order_by(s.storage_journal.c.logical_id).limit(1))
            await conn.execute(text('UPDATE media_objects SET media_path=:path WHERE media_id=:id'),dict(path='music/foreign/changed.mp3',id=object_id))
        with self.assertRaisesRegex(TransferError,'registry_changed'):await self.finish()
        await self.fixture.fixture.originals()

    async def test_corrupt_progress_refuses_before_another_unlink(self):
        from sqlalchemy import update
        await self.step()
        remaining={self.f.target.roots['media']/path.relative_to(self.f.source.roots['media']):data
            for _,path,data in self.fixture.fixture.fixture.resources
            if (self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).exists()}
        self.assertEqual(len(remaining),1)
        async with self.f.target.database.begin() as conn:
            value=await conn.scalar(select(s.units.c.publication));value['target_cancel']['deleted_count']=True
            await conn.execute(update(s.units).values(publication=value))
        with self.assertRaisesRegex(TransferError,'cancel_journal_changed'):await self.step()
        for path,data in remaining.items():self.assertEqual(path.read_bytes(),data)
        await self.originals()


    async def test_stop_fences_old_work_even_at_peak_and_joined_cancellation_keeps_the_lock(self):
        from app.services import resource_cancel_files as files
        grant=await self.grant()
        self.f.traffic.record(egress=100*1024**2)
        self.assertFalse(self.f.traffic.snapshot()['can_run'])
        async with self.f.target.transfers.target_lock:
            result=await self.service.stop(self.f.target.store.node,self.relation,uuid.uuid4().hex,grant)
            self.assertEqual(result['phase'],'waiting_writer')
        result=await self.step();self.assertEqual(result['phase'],'waiting_low_traffic');self.assertEqual(result['deleted_count'],0)
        republish=p.sign(self.f.master.store.unseal(self.f.master.store.node['private_key']),{**grant['payload'],'purpose':'publish'})
        with self.assertRaisesRegex(TransferError,'cancellation_started'):
            await self.f.target.transfers._bind(republish,'target',expected='publish',authenticated_relation=self.relation)
        self.f.traffic._clock=lambda:1100;self.f.traffic._quiet_since=1000
        self.assertTrue(self.f.traffic.snapshot()['can_run'])
        entered=threading.Event();release=threading.Event();original=files.unlink_object
        def held(*args):
            entered.set()
            if not release.wait(5):raise RuntimeError('Controlled unlink was not released')
            return original(*args)
        task=None
        try:
            with patch.object(files,'unlink_object',side_effect=held):
                task=asyncio.create_task(self.step())
                self.assertTrue(await asyncio.to_thread(entered.wait,5));task.cancel();await asyncio.sleep(.05)
                self.assertTrue(self.f.target.transfers.target_lock.locked());self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):await task
        finally:
            release.set()
            if task is not None and not task.done():await asyncio.gather(task,return_exceptions=True)
        await self.finish();await self.originals()


@unittest.skipUnless(os.name=='posix','partial publication rollback requires native Linux inodes')
class CancellationTargetStagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.test_resource_publish import PublicationTests
        from app.services.resource_cancel_source import CancellationSource
        self.pub=PublicationTests('runTest');await self.pub.asyncSetUp();await self.pub.prepare(pair=True)
        self.f=self.pub.f;self.unit=self.pub.unit_id
        self.source=CancellationSource(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.service=TargetCancellation(self.f.target.transfers)
        async with self.f.target.database.connect() as conn:self.relation=dict((await conn.execute(select(fs.relationships))).mappings().one())

    async def asyncTearDown(self):await self.pub.asyncTearDown()

    async def cancel(self):
        from sqlalchemy import update
        async with self.f.master.database.begin() as conn:
            revision=await conn.scalar(select(s.tasks.c.revision))
            await conn.execute(update(s.tasks).values(state='cancelling',revision=revision+1));await conn.execute(update(s.runners).values(expires_at=0))
        self.lease=await self.source.transfer.journal.claim(self.pub.lease.task_id,uuid.uuid4().hex,ttl=60)
        for _ in range(20):
            result=await self.source.advance(self.lease,self.unit)
            if result['proof'] is not None:self.proof=result['proof'];return
        self.fail('Source proof did not converge')

    async def step(self):
        async with self.f.master.database.begin() as conn:task=await self.source.transfer.journal._fenced(conn,self.lease)
        relations=await self.source.transfer.relations(task)
        grant=self.source.transfer._grant(task,relations,self.lease.epoch,self.lease.runner_id,'cancel')
        return await self.service.advance(self.f.target.store.node,self.relation,uuid.uuid4().hex,grant,self.unit,self.proof)

    finish=CancellationTargetTests.finish
    domain=CancellationTargetTests.domain

    async def verify(self):
        before=await self.domain();result=await self.finish();self.assertEqual(result['deleted_count'],2)
        self.assertEqual(await self.domain(),before)
        for _,path,data in self.pub.resources:self.assertEqual(path.read_bytes(),data)
        async with self.f.target.database.connect() as conn:self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM media_objects')),0)

    async def test_verified_private_copies_cancel_before_publication_starts(self):
        await self.cancel();await self.verify()

    async def test_failed_ready_preparation_reclaims_only_its_known_empty_directory(self):
        from app.services import resource_publish_files as files
        original=files.prepare
        def lose(*args):original(*args);raise OSError('controlled ready preparation reply loss')
        with patch.object(files,'prepare',side_effect=lose):
            for _ in range(12):
                try:await self.pub.step()
                except OSError:break
            else:self.fail('Ready preparation fault was not reached')
        self.assertTrue((self.pub.private/'ready').is_dir())
        await self.cancel();await self.verify()

    async def test_interrupted_two_link_assembly_is_reconciled_before_owned_shadow_unlink(self):
        from app.services import resource_publish_files as files
        await self.pub.reach('assembling');original=files.os.unlink
        def lose(name,*args,**kwargs):
            if str(name).endswith('.part'):raise OSError('controlled interruption after hard link')
            return original(name,*args,**kwargs)
        with patch.object(files.os,'unlink',side_effect=lose):
            with self.assertRaises(OSError):await self.pub.step()
        self.assertTrue(any(path.stat().st_nlink==2 for path in (self.pub.private/'ready').iterdir()))
        await self.cancel();await self.verify()

    async def test_lost_directory_move_reply_cancels_the_real_public_location(self):
        from app.services import resource_publish_files as files
        await self.pub.reach('move');original=files.move_directory
        def lose(*args):original(*args);raise OSError('controlled public rename reply loss')
        with patch.object(files,'move_directory',side_effect=lose):
            with self.assertRaises(OSError):await self.pub.step()
        self.assertTrue(self.pub.target.is_dir());self.assertFalse((self.pub.private/'ready').exists())
        await self.cancel();await self.verify()

    async def test_partial_registry_publication_cancels_registered_and_unregistered_files(self):
        await self.pub.reach('register');await self.pub.step()
        async with self.f.target.database.connect() as conn:self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM media_objects')),1)
        await self.cancel();await self.verify()


@unittest.skipUnless(os.name=='posix','opaque recording rollback requires native Linux files')
class CancellationTargetRecordingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.test_resource_cutover import CutoverTests
        from app.services.resource_cancel_source import CancellationSource
        self.base=CutoverTests('runTest');await self.base.asyncSetUp();self.pub=await self.base.prepare_recordings()
        self.f=self.pub.f;self.unit=self.pub.unit_id
        self.source=CancellationSource(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.service=TargetCancellation(self.f.target.transfers)
        async with self.f.target.database.connect() as conn:self.relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
        await self.cancel()

    async def asyncTearDown(self):await self.base.asyncTearDown()

    cancel=CancellationTargetStagesTests.cancel
    step=CancellationTargetStagesTests.step
    finish=CancellationTargetTests.finish
    domain=CancellationTargetTests.domain

    async def test_recording_and_opaque_encrypted_footer_preserve_source_and_master_records(self):
        from app.services import karaoke_schema as ks
        before=await self.domain()
        async with self.f.master.database.connect() as conn:records=[dict(row) for row in (await conn.execute(select(ks.recordings))).mappings()]
        with (patch.object(media_crypto,'open_premaster',side_effect=AssertionError('recording premaster opened')),
                patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('recording decrypted'))):
            result=await self.finish()
        self.assertEqual(result['deleted_count'],3);self.assertEqual(await self.domain(),before)
        for recording,path,data in self.base.resources:
            self.assertEqual(path.read_bytes(),data)
            target=self.f.target.roots['recordings']/self.f.target.relationship/path.parent.name/(recording+'.bin')
            self.assertFalse(target.exists())
        async with self.f.master.database.connect() as conn:self.assertEqual([dict(row) for row in (await conn.execute(select(ks.recordings))).mappings()],records)
