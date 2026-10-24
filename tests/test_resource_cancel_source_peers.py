"""Native Source retention after actual Target rollback; no task settlement."""
import asyncio
import copy
import os
import threading
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete,insert,select,update

from app.services import media_crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_source_peers import SourceCancellation,selected,verify_receipt
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_target as fixtures


@unittest.skipUnless(os.name=='posix','Source retention verifies actual native files')
class SourceRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.target=fixtures.CancellationTargetTests('runTest');await self.target.asyncSetUp()
        self.f,self.unit=self.target.f,self.target.unit
        self.target_receipt=(await self.target.finish())['receipt']
        self.service=SourceCancellation(self.f.source.transfers);self.extra=[]
        async with self.f.source.database.connect() as conn:self.relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
        self.authorization,_,_=await self.target.fixture.service.authority.issue(self.target.lease,self.unit)

    async def asyncTearDown(self):
        for service in self.extra:await service.close()
        await self.target.asyncTearDown()

    async def stop(self):
        self.grant=await self.target.grant()
        return await self.service.stop(self.f.source.store.node,self.relation,uuid.uuid4().hex,self.grant)

    async def step(self,*,replay=False):
        self.grant=await self.target.grant()
        return await self.service.advance(self.f.source.store.node,self.relation,uuid.uuid4().hex,self.grant,self.unit,
            None if replay else self.authorization,None if replay else self.target_receipt)

    async def finish(self):
        for _ in range(24):
            result=await self.step()
            if result['phase']=='retained':return result
        self.fail('Native Source retention did not converge')

    async def unit_row(self):
        async with self.f.source.database.connect() as conn:return dict((await conn.execute(select(s.units))).mappings().one())

    async def test_plain_and_ciphertext_originals_keys_business_and_capacity_remain_unchanged(self):
        before=await self.target.domain();self.assertEqual((await self.stop())['phase'],'stopped')
        with (patch.object(media_crypto,'open_premaster',side_effect=AssertionError('Source premaster opened')),
                patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('Source decrypted media'))):
            result=await self.finish()
        self.assertEqual(await self.target.domain(),before);await self.target.originals()
        self.assertFalse(result['source_files_removed']);self.assertFalse(result['ownership_changed']);self.assertFalse(result['capacity_settled'])
        unit=await self.unit_row();self.assertEqual(unit['state'],'cancelled')
        value=verify_receipt(result['receipt'],self.relation['peer_key'],self.grant['payload'],selected(unit,self.grant['payload']),self.f.now,
            authorization=self.authorization,target_receipt=self.target_receipt,proof=unit['publication']['source_verification'])
        self.assertEqual(value['object_count'],2)
        private=self.f.source.store.unseal(self.f.source.store.node['private_key'])
        for changed in ({'version':True},{'epoch':True},{'object_count':True},{'node_id':'f'*32},{'capacity_settled':0},
                {'source_files_removed':0},{'ownership_changed':0},{'unit_sha256':'0'*64},{'target_receipt_sha256':'0'*64}):
            with self.subTest(changed=changed),self.assertRaises(TransferError):
                verify_receipt(p.sign(private,{**value,**changed}),self.relation['peer_key'],self.grant['payload'],
                    selected(unit,self.grant['payload']),self.f.now,authorization=self.authorization,target_receipt=self.target_receipt,
                    proof=unit['publication']['source_verification'])

    async def test_stop_is_required_before_any_source_retention_census(self):
        before=await self.target.domain()
        with self.assertRaisesRegex(TransferError,'source_stop_required'):await self.step()
        self.assertNotIn('cancel_retained',(await self.unit_row())['publication']);self.assertEqual(await self.target.domain(),before)
        await self.target.originals()

    async def test_missing_forged_and_expired_permissions_or_target_receipts_refuse(self):
        await self.stop();before=await self.target.domain();authorization,receipt=self.authorization,self.target_receipt
        for permission,target in ((None,receipt),(authorization,None),
                (p.sign(self.f.target.store.unseal(self.f.target.store.node['private_key']),authorization['payload']),receipt),
                (authorization,p.sign(self.f.master.store.unseal(self.f.master.store.node['private_key']),receipt['payload']))):
            self.authorization,self.target_receipt=permission,target
            with self.assertRaises(TransferError):await self.step()
        self.authorization,self.target_receipt=authorization,receipt;self.f.now+=46
        with self.assertRaisesRegex(TransferError,'authorization_expired'):await self.step()
        self.assertEqual(await self.target.domain(),before);await self.target.originals()

    async def test_changed_source_is_preserved_and_cannot_receive_a_retention_receipt(self):
        await self.stop();_,path,_=self.target.fixture.fixture.fixture.resources[0]
        path.write_bytes(b'preserve externally changed Source data');before=await self.target.domain()
        with self.assertRaisesRegex(TransferError,'source_changed'):await self.finish()
        self.assertEqual(path.read_bytes(),b'preserve externally changed Source data');self.assertEqual(await self.target.domain(),before)
        self.assertNotEqual((await self.unit_row())['state'],'cancelled');self.assertIsNone(self.f.source.transfers.source_verification)

    async def test_unknown_source_sibling_is_preserved_then_explicit_fixture_resolution_retries(self):
        await self.stop();_,path,_=self.target.fixture.fixture.fixture.resources[0]
        unknown=path.with_name('external-unknown.bin');unknown.write_bytes(b'keep this external file');before=await self.target.domain()
        with self.assertRaisesRegex(TransferError,'unknown_entry'):await self.finish()
        self.assertEqual(unknown.read_bytes(),b'keep this external file');self.assertEqual(await self.target.domain(),before)
        retained=self.f.root/'retained-fixture-injection';unknown.rename(retained)
        self.assertEqual((await self.finish())['phase'],'retained');self.assertEqual(retained.read_bytes(),b'keep this external file')
        await self.target.originals()

    async def test_stop_fences_old_copy_even_at_peak_and_waits_for_the_actual_source_lock(self):
        self.f.traffic.record(egress=100*1024**2);self.assertFalse(self.f.traffic.snapshot()['can_run'])
        async with self.f.source.transfers.source_lock:self.assertEqual((await self.stop())['phase'],'waiting_writer')
        self.assertEqual((await self.stop())['phase'],'stopped')
        async with self.f.source.database.connect() as conn:
            import json
            binding=json.loads(await conn.scalar(select(s.peer_bindings.c.binding_json).limit(1)))
        grant=p.sign(self.f.master.store.unseal(self.f.master.store.node['private_key']),
            {**self.grant['payload'],'purpose':'copy','object':binding['object']})
        with self.assertRaisesRegex(TransferError,'cancellation_started'):
            await self.f.source.transfers._bind(grant,'source',expected='copy',authenticated_relation=self.relation)
        self.assertEqual((await self.step())['phase'],'waiting_low_traffic');await self.target.originals()
        self.f.traffic._clock=lambda:1100;self.f.traffic._quiet_since=1000
        self.assertEqual((await self.finish())['phase'],'retained')

    async def test_only_live_migration_read_leases_block_stop_and_retention(self):
        async with self.f.source.database.begin() as conn:
            await conn.execute(insert(s.read_leases).values(lease_id='a'*32,member_id=self.f.source.store.node['node_id'],logical_id='b'*64,
                object_id='c'*64,source_version='d'*64,expires_at=int(self.f.now)+30,task_id=self.target.lease.task_id,purpose='migration'))
        self.assertEqual((await self.stop())['phase'],'waiting_reader')
        async with self.f.source.database.begin() as conn:await conn.execute(delete(s.read_leases).where(s.read_leases.c.lease_id=='a'*32))
        await self.stop()
        async with self.f.source.database.begin() as conn:
            await conn.execute(insert(s.read_leases).values(lease_id='e'*32,member_id=self.f.source.store.node['node_id'],logical_id='b'*64,
                object_id='c'*64,source_version='d'*64,expires_at=int(self.f.now)+30,task_id='',purpose='playback'))
        self.assertEqual((await self.finish())['phase'],'retained');await self.target.originals()

    async def test_traffic_spike_during_bounded_metadata_scan_retains_originals_and_can_retry(self):
        await self.stop();opened=self.f.source.transfers.observer._open;count=0
        async def spike(*args,**kwargs):
            nonlocal count
            result=await opened(*args,**kwargs);count+=1
            if count==1:self.f.traffic.record(egress=100*1024**2)
            return result
        with patch.object(self.f.source.transfers.observer,'_open',side_effect=spike):
            with self.assertRaisesRegex(TransferError,'waiting_low_traffic'):await self.step()
        self.assertIsNone(self.f.source.transfers.source_verification);await self.target.originals()
        self.f.traffic._clock=lambda:1100;self.f.traffic._quiet_since=1000
        self.assertEqual((await self.finish())['phase'],'retained')

    async def test_cancelled_scan_joins_native_io_before_releasing_the_source_lock(self):
        from app.services.resource_source_verify import SourceScan
        await self.stop();await self.step();self.assertEqual((await self.step())['phase'],'membership')
        entered=threading.Event();release=threading.Event();page=SourceScan.page;task=None
        def held(scan):
            entered.set()
            if not release.wait(5):raise RuntimeError('Controlled metadata scan was not released')
            return page(scan)
        try:
            with patch.object(SourceScan,'page',held):
                task=asyncio.create_task(self.step());self.assertTrue(await asyncio.to_thread(entered.wait,5));task.cancel()
                await asyncio.sleep(.05);self.assertFalse(task.done());self.assertTrue(self.f.source.transfers.source_lock.locked())
                release.set()
                with self.assertRaises(asyncio.CancelledError):await task
        finally:
            release.set()
            if task is not None and not task.done():await asyncio.gather(task,return_exceptions=True)
        self.assertFalse(self.f.source.transfers.source_lock.locked());self.assertIsNone(self.f.source.transfers.source_verification)
        self.assertEqual((await self.finish())['phase'],'retained');await self.target.originals()

    async def test_final_audit_failure_keeps_verified_originals_and_retry_issues_one_receipt(self):
        await self.stop();before=await self.target.domain();log=self.f.source.store.log
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_source_unit_retained':raise RuntimeError('controlled Source retention audit rollback')
            return await log(conn,action,*args,**kwargs)
        with patch.object(self.f.source.store,'log',side_effect=fail):
            with self.assertRaises(RuntimeError):await self.finish()
        self.assertEqual((await self.unit_row())['state'],'verified');self.assertNotIn('cancel_retained',(await self.unit_row())['publication'])
        self.assertEqual(await self.target.domain(),before);self.assertEqual((await self.finish())['phase'],'retained');await self.target.originals()

    async def test_new_transfer_instance_and_epoch_replay_the_same_receipt_without_touching_later_material(self):
        await self.stop();result=await self.finish();before=await self.target.domain()
        _,path,_=self.target.fixture.fixture.fixture.resources[0];later=path.with_name('later-external.bin');later.write_bytes(b'preserve later material')
        await self.f.source.transfers.close()
        self.f.source.transfers=TransferPeers(self.f.source.store,root_provider=lambda:self.f.source.roots,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.extra.append(self.f.source.transfers);self.service=SourceCancellation(self.f.source.transfers);self.f.now+=100
        async with self.f.master.database.begin() as conn:await conn.execute(update(fs.relationships).values(last_heartbeat=int(self.f.now)))
        self.target.lease=await self.target.fixture.service.transfer.journal.claim(self.target.lease.task_id,uuid.uuid4().hex,ttl=60)
        replay=await self.step(replay=True);self.assertEqual(replay['receipt'],result['receipt'])
        self.assertEqual(later.read_bytes(),b'preserve later material');self.assertEqual(await self.target.domain(),before);await self.target.originals()

    async def test_malformed_terminal_records_refuse_without_modifying_files_or_capacity(self):
        await self.stop();result=await self.finish();original=copy.deepcopy((await self.unit_row())['publication']);before=await self.target.domain()
        record=original['cancel_retained']
        for changed in (None,[],dict(record,version=True),dict(record,authorization=None),dict(record,target_receipt=None),dict(record,receipt=None)):
            async with self.f.source.database.begin() as conn:await conn.execute(update(s.units).values(publication={**original,'cancel_retained':changed}))
            with self.assertRaises(TransferError):await self.step(replay=True)
            self.assertEqual(await self.target.domain(),before);await self.target.originals()
        async with self.f.source.database.begin() as conn:await conn.execute(update(s.units).values(publication={**original,'source_verification':None}))
        with self.assertRaises(TransferError):await self.step(replay=True)
        async with self.f.source.database.begin() as conn:await conn.execute(update(s.units).values(publication=original))
        self.assertEqual((await self.step(replay=True))['receipt'],result['receipt'])

    async def test_changed_root_refuses_fresh_retention_and_preserves_both_directories(self):
        await self.stop();old=self.f.source.roots['media'];replacement=self.f.root/'replacement-media';replacement.mkdir();marker=replacement/'keep.bin';marker.write_bytes(b'keep new root')
        self.f.source.roots['media']=replacement
        with self.assertRaisesRegex(TransferError,'source_root_changed'):await self.step()
        self.assertEqual(marker.read_bytes(),b'keep new root');self.assertTrue(old.is_dir());await self.target.originals()

    async def test_actual_asgi_hmac_stop_and_retention_reject_nonce_replay(self):
        import httpx
        from fastapi import FastAPI
        from app.api import internal_nodes,internal_resource_inventory,internal_resource_transfer as api
        application=FastAPI();application.include_router(api.router);key=p.public_key(self.f.source.store.unseal(self.f.source.store.node['private_key']))
        with (patch.object(internal_nodes,'state',self.f.source.store),patch.object(internal_resource_inventory,'state',self.f.source.store),
                patch.object(api,'transfers',self.f.source.transfers)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url=self.f.source.store.node['endpoint']) as client:
                for index in range(25):
                    path='/internal/v1/migration/transfer/'+('cancel-source-stop' if index==0 else 'cancel-source-retain')
                    value=dict(request_id=uuid.uuid4().hex,grant=await self.target.grant())
                    if index:value.update(unit_id=self.unit,authorization=self.authorization,target_receipt=self.target_receipt)
                    raw=p.canonical(value);headers=p.auth_headers(self.f.source.credential,self.f.source.relationship,'POST',path,raw)
                    response=await client.post(path,content=raw,headers=headers);self.assertEqual(response.status_code,200,response.text)
                    result=p.verify(key,response.json());self.assertEqual(result['request_id'],value['request_id'])
                    if index==0:self.assertEqual((await client.post(path,content=raw,headers=headers)).status_code,401)
                    if result['phase']=='retained':self.assertIsNotNone(result['receipt']);break
                else:self.fail('Authenticated Source retention did not converge')
        await self.target.originals()


@unittest.skipUnless(os.name=='posix','Source commit guard uses actual native acknowledgements')
class SourceRetentionCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_source_commit_cannot_be_relabelled_as_retained(self):
        from app.services.resource_commit_peers import CommitPeers
        target=fixtures.CancellationTargetTests('runTest');await target.asyncSetUp()
        try:
            await target.test_actual_native_commit_refuses_an_older_valid_cancellation_permission();f=target.f
            async with f.source.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            async with f.master.database.connect() as conn:record=(await conn.scalar(select(s.units.c.publication)))['commit']
            cancel=await target.grant();commit=p.sign(f.master.store.unseal(f.master.store.node['private_key']),{**cancel['payload'],'purpose':'commit'})
            await CommitPeers(f.source.transfers).acknowledge(f.source.store.node,relation,uuid.uuid4().hex,commit,record)
            service=SourceCancellation(f.source.transfers);await service.stop(f.source.store.node,relation,uuid.uuid4().hex,cancel)
            before=await target.domain()
            with self.assertRaisesRegex(TransferError,'native_commit_is_final'):
                await service.advance(f.source.store.node,relation,uuid.uuid4().hex,cancel,target.unit,None,None)
            self.assertEqual(await target.domain(),before);await target.originals()
        finally:await target.asyncTearDown()


@unittest.skipUnless(os.name=='posix','recording retention checks opaque native footers')
class SourceRetentionRecordingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_lyric_and_encrypted_lyric_footers_retain_exact_source_bytes(self):
        target=fixtures.CancellationTargetRecordingsTests('runTest');await target.asyncSetUp()
        try:
            await target.finish();f=target.f;before=await target.domain()
            async with f.source.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            authorization,raw,_=await target.source.authority.issue(target.lease,target.unit)
            service=SourceCancellation(f.source.transfers);grant=p.sign(f.master.store.unseal(f.master.store.node['private_key']),raw)
            async with f.target.database.connect() as conn:receipt=(await conn.scalar(select(s.units.c.publication)))['target_cancel']['receipt']
            await service.stop(f.source.store.node,relation,uuid.uuid4().hex,grant)
            with (patch.object(media_crypto,'open_premaster',side_effect=AssertionError('recording premaster')),
                    patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('recording decryption'))):
                for _ in range(24):
                    result=await service.advance(f.source.store.node,relation,uuid.uuid4().hex,grant,target.unit,authorization,receipt)
                    if result['phase']=='retained':break
                else:self.fail('Opaque recording Source retention did not converge')
            self.assertEqual(await target.domain(),before)
            for _,path,data in target.base.resources:self.assertEqual(path.read_bytes(),data)
            self.assertEqual(result['processed_objects'],3)
        finally:await target.asyncTearDown()
