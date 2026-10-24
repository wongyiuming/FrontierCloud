"""Real stores/files and separate Source ASGI bytes; in-process TLS is not proof."""
import asyncio
import copy
import hashlib
import json
import os
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI
import httpx
from sqlalchemy import delete, func, insert, select, update

from app.api import internal_nodes, internal_resource_transfer as api
from app.services import media_crypto as crypto, resource_migration_schema as s
from app.services.federation import protocol as p, schema as fs
from app.services.resource_transfer_files import DIRECTORY
from app.services.resource_transfer_master import TransferMaster
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import (SOURCE_PATH,WINDOW_BYTES,TransferError,grant_shape,target_request)
from tests import test_resource_migration_admission as fixtures


class ObjectEncryptionBindingTests(unittest.TestCase):
    def object(self):
        from app.services.resource_observation import digest
        obj=dict(logical_id='a'*64,unit_id=digest(['media','b'*32,'music/album']),resource_kind='media',
            source_object='c'*64,source_path='music/album/song.mp3',target_path='music/album/song.mp3',
            source_version='d'*64,unit_version='e'*64,expected_bytes=100,sha256='f'*64,
            metadata_sha256='a'*64,encryption_sha256=digest(None),user_id='')
        return dict(source_id='b'*32,source_relationship='1'*32,target_relationship='2'*32,object=obj)

    def test_previous_objects_and_explicit_master_only_descriptor_are_valid(self):
        from app.services.resource_transfer_protocol import object_shape,target_encryption_digest
        value=self.object();self.assertIs(object_shape(value),value)
        value['object']['master_encryption_sha256']='b'*64
        self.assertIs(object_shape(value),value)
        self.assertEqual(target_encryption_digest(value['object']),'b'*64)

    def test_unknown_or_malformed_additional_authority_is_refused(self):
        from app.services.resource_transfer_protocol import object_shape
        for change in ({'master_encryption_sha256':True},{'master_encryption_sha256':'bad'},
                {'master_encryption_sha256':'b'*64,'unknown_authority':'c'*64}):
            with self.subTest(change=change):
                value=self.object();value['object'].update(change)
                with self.assertRaises(TransferError):object_shape(value)

    def test_different_nonempty_native_descriptor_cannot_be_relabelled_by_master_field(self):
        from app.services.resource_transfer_protocol import object_shape
        value=self.object();value['object'].update(encryption_sha256='a'*64,master_encryption_sha256='b'*64)
        with self.assertRaises(TransferError):object_shape(value)


@unittest.skipUnless(os.name=='posix','direct transfer requires Linux nofollow paths')
class ResourceTransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.MigrationAdmissionTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f;self.services=[];self.patches=[];self.raw_requests=[];self.grants=[]
        for peer in (self.f.source,self.f.target):
            peer.transfers=TransferPeers(peer.store,root_provider=lambda peer=peer:peer.roots,traffic=self.f.traffic,wall=lambda:self.f.now)
            self.services.append(peer.transfers)
        self.original_request=self.f.transport.request;self.f.transport.request=self.control
        self.master=TransferMaster(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        app=FastAPI();app.include_router(api.router)
        self.patches=[patch.object(api,'transfers',self.f.source.transfers),patch.object(internal_nodes.settings,'TLS_ENABLED',True)]
        for item in self.patches:item.start()
        self.source_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=self.f.source.store.node['endpoint'])
        self.f.target.transfers.client=self.source_client;self.f.target.transfers.owns_client=False

    async def asyncTearDown(self):
        await self.source_client.aclose()
        for service in self.services:await service.close()
        for item in reversed(self.patches):item.stop()
        await self.fixture.asyncTearDown()

    async def control(self,origin,path,**kwargs):
        if '/migration/transfer/' not in path:return await self.original_request(origin,path,**kwargs)
        peer=self.f.source if origin==self.f.source.store.node['endpoint'] else self.f.target
        self.assertEqual(origin,peer.store.node['endpoint'])
        self.assertEqual((kwargs['credential'],kwargs['relation']),(peer.credential,peer.relationship))
        self.raw_requests.append(p.canonical(kwargs['value']));self.grants.append(kwargs['value']['grant'])
        async with peer.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==peer.relationship))).mappings().one())
        result=await getattr(peer.transfers,path.rsplit('/',1)[-1])(peer.store.node,relation,**kwargs['value'])
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)

    async def prepare(self,*,large=False,encrypted=False):
        data=b'ID3-original'+(b'managed payload\x00'*((2*WINDOW_BYTES)//16) if large else b' original'*300)
        self.logical,self.original,self.data=await self.f.media(data,encrypted=encrypted)
        self.plan=await self.f.plan();self.assertEqual(self.plan['state'],'ready',self.plan)
        await self.fixture.admission.prepare(self.plan['plan_id'],self.plan['review_sha256'],'test-admin')
        return self.plan

    async def queue(self):
        result=await self.master.preflight(self.plan['plan_id'],'test-admin');self.assertEqual(result['state'],'queued')
        self.lease=await self.master.start(self.plan['plan_id'],uuid.uuid4().hex)
        return result

    async def step(self):
        self.lease,result=await self.master.copy_one(self.lease,self.logical)
        return result

    def stage(self):
        grant=self.grants[-1]['payload'];obj=grant['object']
        return self.f.target.roots['media']/DIRECTORY/grant['task_id']/obj['unit_id']/(obj['logical_id']+'.part')

    async def test_actual_direct_ranges_verify_all_bytes_without_publication_or_master_payload(self):
        await self.prepare(large=True);await self.queue()
        results=[]
        with (patch.object(crypto,'open_premaster',side_effect=AssertionError('premaster opened')),
              patch.object(crypto.Manager,'file_key',side_effect=AssertionError('media decrypted'))):
            while not results or results[-1]['state']!='verified':results.append(await self.step())
        self.assertGreater(len(results),2)
        self.assertEqual(results[0]['received_bytes'],WINDOW_BYTES)
        self.assertEqual(results[-1]['sha256'],hashlib.sha256(self.data).hexdigest())
        self.assertEqual(self.stage().read_bytes(),self.data);self.assertEqual(self.original.read_bytes(),self.data)
        self.assertFalse((self.f.target.roots['media']/'music/album/song.mp3').exists())
        self.assertLess(sum(len(value) for value in self.raw_requests),len(self.data)//20)
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.storage_journal).where(s.storage_journal.c.state=='verified')),1)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(fs.global_media)),0)
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.global_media.c.storage_member_id)),self.f.source.store.node['node_id'])
            self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),0)
        self.assertTrue(all(len(value)<16*1024 for value in self.raw_requests))

    async def test_ciphertext_staging_preserves_descriptor_and_does_not_need_file_key(self):
        await self.prepare(encrypted=True);await self.queue()
        with patch.object(crypto.Manager,'file_key',side_effect=AssertionError('decryption attempted')):result=await self.step()
        self.assertEqual(result['state'],'verified');self.assertEqual(self.stage().read_bytes(),self.data)
        self.assertEqual(self.original.read_bytes(),self.data)

    async def test_failed_direct_probe_does_not_queue_or_start_copy(self):
        await self.prepare()
        transport=httpx.MockTransport(lambda request:(_ for _ in ()).throw(httpx.ConnectError('unreachable')))
        client=httpx.AsyncClient(transport=transport)
        self.f.target.transfers.client=client
        try:
            with self.assertRaisesRegex(TransferError,'direct_unavailable'):await self.master.preflight(self.plan['plan_id'],'test-admin')
        finally:await client.aclose()
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.tasks.c.state)),'prepared')
            self.assertEqual(await conn.scalar(select(s.tasks.c.error_code)),'migration_transfer_direct_unavailable')
        async with self.f.target.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.storage_journal)),0)
        self.assertFalse((self.f.target.roots['media']/DIRECTORY).exists())

    async def test_source_requires_target_private_key_before_any_durable_change(self):
        await self.prepare();await self.queue();grant=self.grants[0]
        changed=copy.deepcopy(grant);changed['payload']['epoch']+=100
        changed=p.sign(self.f.master.store.unseal(self.f.master.store.node['private_key']),changed['payload'])
        request=target_request(p.new_key(),changed,now=self.f.now)
        async with self.f.source.database.connect() as conn:before=(await conn.execute(select(s.peer_reservations.c.payload))).scalar_one()
        response=await self.source_client.post(SOURCE_PATH,json=request)
        self.assertEqual(response.status_code,409)
        async with self.f.source.database.connect() as conn:self.assertEqual((await conn.execute(select(s.peer_reservations.c.payload))).scalar_one(),before)

    async def test_signed_target_request_replay_and_tampered_range_are_rejected(self):
        await self.prepare();await self.queue();grant=self.grants[0]
        request=target_request(self.f.target.store.unseal(self.f.target.store.node['private_key']),grant,now=self.f.now)
        self.assertEqual((await self.source_client.post(SOURCE_PATH,json=request)).status_code,200)
        self.assertEqual((await self.source_client.post(SOURCE_PATH,json=request)).status_code,409)
        request=target_request(self.f.target.store.unseal(self.f.target.store.node['private_key']),grant,now=self.f.now)
        request['proof']['payload']['length']=1
        self.assertEqual((await self.source_client.post(SOURCE_PATH,json=request)).status_code,409)

    async def test_restart_target_reconciles_existing_partial_bytes_and_keeps_quota(self):
        await self.prepare(large=True);await self.queue();first=await self.step()
        self.assertEqual(first['received_bytes'],WINDOW_BYTES)
        replacement=TransferPeers(self.f.target.store,root_provider=lambda:self.f.target.roots,traffic=self.f.traffic,wall=lambda:self.f.now,
            client=self.source_client)
        self.f.target.transfers=replacement;self.services.append(replacement)
        result=await self.step()
        self.assertGreater(result['received_bytes'],first['received_bytes'])
        while result['state']!='verified':result=await self.step()
        self.assertEqual(self.stage().read_bytes(),self.data)
        self.assertEqual(await self.fixture.quota(),(len(self.data),len(self.data)))

    async def test_previous_plan_shape_resumes_existing_partial_ciphertext_with_identical_grants(self):
        await self.prepare(large=True,encrypted=True)
        async with self.f.master.database.begin() as conn:
            encoded=await conn.scalar(select(s.plan_bindings.c.object_json).where(s.plan_bindings.c.logical_id==self.logical))
            old=json.loads(encoded);old.pop('source_encryption_sha256')
            await conn.execute(update(s.plan_bindings).where(s.plan_bindings.c.logical_id==self.logical).values(object_json=p.canonical(old).decode()))
        await self.queue();result=await self.step();self.assertEqual(result['received_bytes'],WINDOW_BYTES)
        original=self.grants[-1]['payload']['object'];self.assertNotIn('master_encryption_sha256',original)
        self.master=TransferMaster(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        with patch.object(crypto.Manager,'file_key',side_effect=AssertionError('decrypted')):
            for _ in range(8):
                result=await self.step()
                if result['state']=='verified':break
            else:self.fail('Previous immutable object did not resume')
        self.assertEqual(self.grants[-1]['payload']['object'],original)
        self.assertEqual(self.stage().read_bytes(),self.data);self.assertEqual(self.original.read_bytes(),self.data)
        self.assertEqual(await self.fixture.quota(),(len(self.data),len(self.data)))

    async def test_next_chunk_reuses_hash_context_instead_of_reading_prefix_again(self):
        await self.prepare(large=True);await self.queue();await self.step()
        from app.services import resource_transfer_peers as peers
        original=peers.os.pread;staged_reads=[]
        def observed(descriptor,length,offset):
            if os.readlink('/proc/self/fd/'+str(descriptor)).endswith('.part'):staged_reads.append((length,offset))
            return original(descriptor,length,offset)
        with patch.object(peers.os,'pread',side_effect=observed):await self.step()
        self.assertEqual(staged_reads,[])

    async def test_missing_native_capacity_refuses_copy_without_master_progress(self):
        await self.prepare();await self.queue()
        async with self.f.target.database.begin() as conn:await conn.execute(delete(s.capacity))
        with self.assertRaises(p.ProtocolError):await self.step()
        self.assertEqual(self.original.read_bytes(),self.data)
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.objects.c.verified_bytes)),0)

    async def test_no_follow_staging_symlink_is_refused_and_external_file_unchanged(self):
        await self.prepare();await self.queue()
        async with self.f.master.database.connect() as conn:task=await self.master.preparation._task(conn,self.plan['plan_id'])
        outside=self.f.root/'untouched';outside.write_bytes(b'keep original')
        root=self.f.target.roots['media']/DIRECTORY/task['task_id'];unit=(await self.f.planner.status(task['task_id']))['objects'][0]['unit_id']
        (root/unit).mkdir(mode=0o700)
        (root/unit/(self.logical+'.part')).symlink_to(outside)
        with self.assertRaises(OSError):await self.step()
        self.assertEqual(outside.read_bytes(),b'keep original')

    async def test_live_business_traffic_pauses_bytes_without_discarding_partial_file(self):
        await self.prepare(large=True);await self.queue();await self.step();partial=self.stage().read_bytes()
        with patch.object(self.f.target.transfers.traffic,'snapshot',return_value={'can_run':False}):
            with self.assertRaisesRegex(TransferError,'waiting_low_traffic'):await self.step()
        self.assertEqual(self.stage().read_bytes(),partial);self.assertEqual(self.original.read_bytes(),self.data)

    async def test_manual_pause_invalidates_runner_and_never_issues_another_grant(self):
        await self.prepare(large=True);await self.queue();await self.step();count=len(self.grants)
        await self.master.journal.control(self.plan['plan_id'],self.lease.revision,'pause','test-admin')
        with self.assertRaises(p.ProtocolError):await self.step()
        self.assertEqual(len(self.grants),count)

    async def test_stream_not_started_still_releases_descriptor_lock_and_read_lease(self):
        await self.prepare(large=True);await self.queue();await self.step()
        grant=self.grants[-1]
        request=target_request(self.f.target.store.unseal(self.f.target.store.node['private_key']),grant,offset=0,length=1024,now=self.f.now)
        _,handle=await self.f.source.transfers.source(request)
        self.assertTrue(self.f.source.transfers.source_lock.locked())
        await handle.close();await handle.close()
        self.assertFalse(self.f.source.transfers.source_lock.locked())
        async with self.f.source.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.read_leases)),0)

    async def test_expired_or_other_purpose_and_overlarge_ranges_are_refused(self):
        await self.prepare();await self.queue();grant=self.grants[0]['payload']
        for changed in ({**grant,'expires_at':int(self.f.now)-1},{**grant,'purpose':'unsupported'},
                {**grant,'purpose':'publish','object':{}},{**grant,'epoch':True}):
            with self.assertRaises(TransferError):grant_shape(changed,self.f.now)
        publication=p.sign(self.f.master.store.unseal(self.f.master.store.node['private_key']),{**grant,'purpose':'publish'})
        self.assertEqual(grant_shape(publication['payload'],self.f.now)['purpose'],'publish')
        with self.assertRaisesRegex(TransferError,'purpose_invalid'):
            await self.f.source.transfers.source(dict(grant=publication,proof={}))

    async def test_expiry_during_joined_read_does_not_send_data_after_deadline(self):
        await self.prepare(large=True);await self.queue();await self.step();grant=self.grants[-1]
        request=target_request(self.f.target.store.unseal(self.f.target.store.node['private_key']),grant,offset=0,length=1024,now=self.f.now)
        _,handle=await self.f.source.transfers.source(request)
        from app.services import resource_transfer_peers as peers
        original=peers.os.pread
        def delayed(*args):
            value=original(*args);self.f.now+=46;return value
        with patch.object(peers.os,'pread',side_effect=delayed):
            with self.assertRaisesRegex(TransferError,'window_expired'):await anext(handle.body)
        self.assertFalse(self.f.source.transfers.source_lock.locked())
        async with self.f.source.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.read_leases)),0)

    async def test_expired_reader_reap_is_bounded_and_preserves_live_or_other_node_leases(self):
        await self.prepare();await self.queue();grant=self.grants[0]
        async with self.f.source.database.begin() as conn:
            for index in range(3):
                await conn.execute(insert(s.read_leases).values(lease_id=str(index+1)*32,
                    member_id=self.f.source.store.node['node_id'] if index<2 else self.f.target.store.node['node_id'],
                    logical_id=self.logical,object_id='a'*64,source_version='b'*64,
                    expires_at=int(self.f.now)+(100 if index==1 else -1)))
        request=target_request(self.f.target.store.unseal(self.f.target.store.node['private_key']),grant,now=self.f.now)
        reply,handle=await self.f.source.transfers.source(request);self.assertIsNone(handle)
        async with self.f.source.database.connect() as conn:
            self.assertEqual(set((await conn.execute(select(s.read_leases.c.lease_id))).scalars()),{'2'*32,'3'*32})
