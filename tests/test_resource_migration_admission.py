"""Three real SQL stores and files; signed in-process controls, not TLS proof."""
import asyncio
import hashlib
import os
import unittest
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import delete, func, insert, select, update

from app.services import media_crypto as crypto, media_manager, resource_fences
from app.services import resource_migration_schema as s
from app.services.federation import protocol as p, schema as fs
from app.services.resource_migration_admission import Admission, AdmissionError
from app.services.resource_migration_journal import Journal
from app.services.resource_peer_reservations import NativeReservations, ReservationError
from app.services.resource_plan_store import PlanError
from app.services.resource_registry import RegistryClient, RegistryError
from app.services.resource_reservation_client import ReservationClient
from tests import test_resource_peer_reservations as fixtures


@unittest.skipUnless(os.name=='posix','admission observes native Linux files')
class MigrationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.PeerReservationTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f
        self.original_request=self.f.transport.request
        self.f.transport.request=self.request
        self.after=None
        self.admission=self.new_admission(epoch=self.f.planner.epoch)

    def new_admission(self,*,epoch=None):
        return Admission(self.f.master.store,self.f.transport,preview_epoch=epoch,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def asyncTearDown(self):await self.fixture.asyncTearDown()

    async def request(self,origin,path,**kwargs):
        if '/reservations/' not in path and not path.endswith('/reserved-media'):
            return await self.original_request(origin,path,**kwargs)
        peer=next(peer for peer in self.f.nodes.values() if peer.store.node['endpoint']==origin)
        self.assertEqual((kwargs['relation'],kwargs['credential']),(peer.relationship,peer.credential))
        value=kwargs['value'];self.f.calls.append((peer.name,path,dict(value)))
        if self.f.hook:await self.f.hook(peer,path,value)
        async with peer.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==peer.relationship))).mappings().one())
        operation=peer.registry.reserved_page if path.endswith('/reserved-media') else getattr(peer.reservations,
            {'unit':'add_unit'}.get(path.rsplit('/',1)[-1],path.rsplit('/',1)[-1].replace('-','_')))
        with patch.object(media_manager,'MEDIA_ROOT',peer.roots['media']):
            result=await operation(peer.store.node,relation,**value)
        if self.after:await self.after(peer,path,value,result)
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)

    async def preview(self,*,many=False):
        self.originals=[await self.f.media(b'ID3 original'),await self.f.media(b'private ciphertext'*40,
            path='music/second/private.mp3',encrypted=True)]
        if many:
            for index in range(18):self.originals.append(await self.f.media(b'ID3'+bytes([index]),path=f'music/album/{index:03}.mp3'))
        self.plan=await self.f.plan();self.assertEqual(self.plan['state'],'ready',self.plan)
        return self.plan

    async def prepare(self,service=None):
        return await (service or self.admission).prepare(self.plan['plan_id'],self.plan['review_sha256'],'test-admin')

    async def quota(self):
        async with self.f.master.database.connect() as conn:
            master=await conn.scalar(select(fs.storage_members.c.reserved_bytes).where(fs.storage_members.c.member_id==self.f.target.store.node['node_id']))
        return master,await self.fixture.quota(self.f.target)

    async def assert_originals(self):
        for _,path,raw in self.originals:self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(list(self.f.target.roots['media'].rglob('*.mp3')),[])

    async def test_whole_parent_preparation_never_authorizes_transfer_or_opens_keys(self):
        await self.preview(many=True)
        with (patch.object(crypto,'open_premaster',side_effect=AssertionError('premaster opened')),
              patch.object(crypto.Manager,'file_key',side_effect=AssertionError('ciphertext decrypted'))):
            status=await self.prepare()
        self.assertEqual((status['state'],status['object_count'],status['unit_count']),('prepared',20,2))
        self.assertEqual(await self.quota(),(self.plan['total_bytes'],)*2)
        self.assertFalse(status['writable_verified']);self.assertFalse(status['execution_authorized'])
        with self.assertRaises(p.ProtocolError):await Journal(self.f.master.store).claim(self.plan['plan_id'],uuid.uuid4().hex)
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),4)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.objects)),0)
            self.assertEqual(await conn.scalar(select(s.drains.c.member_id)),self.f.source.store.node['node_id'])
        await self.assert_originals()

    async def test_expired_or_different_epoch_preview_has_no_authority(self):
        await self.preview();self.f.now+=301
        with self.assertRaisesRegex(AdmissionError,'preview_invalid'):await self.prepare()
        self.assertEqual(await self.quota(),(0,0))
        self.f.now-=301
        with self.assertRaisesRegex(AdmissionError,'preview_invalid'):await self.prepare(self.new_admission())
        with self.assertRaisesRegex(AdmissionError,'preview_invalid'):
            await self.admission.prepare(self.plan['plan_id'],'a'*64,'test-admin')

    async def test_source_changed_after_preview_keeps_fences_and_original_file(self):
        await self.preview();_,path,raw=self.originals[0];path.write_bytes(raw+b'changed')
        with self.assertRaises(PlanError):await self.prepare()
        status=await self.admission.status(self.plan['plan_id'])
        self.assertEqual(status['state'],'admitting');self.assertTrue(status['error_code'])
        self.assertEqual(await self.quota(),(self.plan['total_bytes'],)*2)
        self.assertEqual(path.read_bytes(),raw+b'changed')

    async def test_target_parent_conflict_does_not_delete_conflicting_file(self):
        await self.preview();path=self.f.target.roots['media']/'music/album/unknown.bin'
        path.parent.mkdir(parents=True);path.write_bytes(b'user file')
        with self.assertRaisesRegex(PlanError,'target_parent_occupied'):await self.prepare()
        await self.admission.cancel_unstarted(self.plan['plan_id'],'test-admin')
        self.assertEqual(path.read_bytes(),b'user file');self.assertEqual(await self.quota(),(0,0))

    async def test_lost_begin_reply_retries_same_native_authority_after_new_coordinator(self):
        await self.preview();lost=False
        async def after(peer,path,value,result):
            nonlocal lost
            if peer is self.f.source and path.endswith('/begin') and not lost:
                lost=True;raise httpx.ReadError('lost acknowledgement')
        self.after=after
        with self.assertRaises(httpx.ReadError):await self.prepare()
        self.after=None
        status=await self.prepare(self.new_admission())
        self.assertEqual(status['state'],'prepared');self.assertEqual(await self.quota(),(self.plan['total_bytes'],)*2)
        await self.assert_originals()

    async def test_partial_native_scopes_exclude_ordinary_mutation_before_retry(self):
        await self.preview();lost=False
        async def after(peer,path,value,result):
            nonlocal lost
            if peer is self.f.target and path.endswith('/unit') and not lost:
                lost=True;raise httpx.ReadError('lost unit acknowledgement')
        self.after=after
        with self.assertRaises(httpx.ReadError):await self.prepare()
        for peer in (self.f.master,self.f.target):
            async with peer.database.begin() as conn:
                with self.assertRaises(resource_fences.FenceConflict):
                    await resource_fences.paths(conn,'media',[('music/not-yet-scoped/song.mp3',False)])
        self.after=None
        self.assertEqual((await self.prepare(self.new_admission()))['state'],'prepared')

    async def test_missing_begin_cancel_creates_tombstones_even_with_no_free_space(self):
        await self.preview()
        with patch.object(self.admission,'_relations',side_effect=AdmissionError('migration_test_interruption')):
            with self.assertRaises(AdmissionError):await self.prepare()
        with patch('app.services.resource_peer_reservations._capacity',side_effect=AssertionError('cancel probed disk')):
            status=await self.admission.cancel_unstarted(self.plan['plan_id'],'test-admin')
        self.assertEqual(status['state'],'cancelled');self.assertEqual(await self.quota(),(0,0))
        self.assertEqual(status['peers'],dict(source='aborted',target='aborted'))
        async with self.f.master.database.connect() as conn:task=await self.admission._task(conn,self.plan['plan_id'])
        for side in ('source','target'):
            peer=getattr(self.f,side);request=self.admission._request(task,side)
            result=await peer.reservations.begin(peer.store.node,await self.fixture.relation(peer),uuid.uuid4().hex,request)
            self.assertEqual(result['state'],'aborted')
        await self.assert_originals()

    async def test_lost_abort_reply_cannot_release_master_capacity_early(self):
        await self.preview();await self.prepare();lost=False
        async def after(peer,path,value,result):
            nonlocal lost
            if peer is self.f.source and path.endswith('/cancel-unstarted') and not lost:
                lost=True;raise httpx.ReadError('lost abort acknowledgement')
        self.after=after
        with self.assertRaises(httpx.ReadError):await self.admission.cancel_unstarted(self.plan['plan_id'],'test-admin')
        self.assertEqual(await self.quota(),(self.plan['total_bytes'],)*2)
        self.after=None
        status=await self.new_admission().cancel_unstarted(self.plan['plan_id'],'test-admin')
        self.assertEqual(status['state'],'cancelled');self.assertEqual(await self.quota(),(0,0))
        self.assertEqual((await self.admission.cancel_unstarted(self.plan['plan_id'],'test-admin'))['state'],'cancelled')

    async def test_failed_final_abort_audit_rolls_back_master_only_and_retries(self):
        await self.preview();await self.prepare();original=self.f.master.store.log
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_admission_cancelled':raise OSError('audit unavailable')
            return await original(conn,action,*args,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.admission.cancel_unstarted(self.plan['plan_id'],'test-admin')
        self.assertEqual(await self.quota(),(self.plan['total_bytes'],0))
        self.assertEqual((await self.admission.status(self.plan['plan_id']))['state'],'admission_aborting')
        await self.admission.cancel_unstarted(self.plan['plan_id'],'test-admin')
        self.assertEqual(await self.quota(),(0,0));await self.assert_originals()

    async def test_missing_master_reservation_is_not_replaced_by_plan_counters(self):
        await self.preview();await self.prepare()
        async with self.f.master.database.begin() as conn:await conn.execute(delete(s.capacity).where(s.capacity.c.task_id==self.plan['plan_id']))
        with self.assertRaisesRegex(AdmissionError,'reservation_inconsistent'):
            await self.admission.cancel_unstarted(self.plan['plan_id'],'test-admin')
        self.assertEqual((await self.quota())[0],self.plan['total_bytes'])

    async def test_no_implicit_abort_after_transfer_material_appears(self):
        await self.preview();await self.prepare()
        async with self.f.master.database.begin() as conn:
            await conn.execute(insert(s.peer_bindings).values(task_id=self.plan['plan_id'],logical_id='a'*64,
                unit_id='c'*64,source_path='music/album/song.mp3',binding_sha256='b'*64,binding_json='{}'))
        with self.assertRaisesRegex(AdmissionError,'requires_reconciliation'):
            await self.admission.cancel_unstarted(self.plan['plan_id'],'test-admin')
        self.assertEqual(await self.quota(),(self.plan['total_bytes'],)*2)

    async def test_restart_after_incomplete_final_scan_rechecks_without_growing_spool(self):
        await self.preview();old=self.admission.spool.native_entry;count=0
        async def interrupted(*args,**kwargs):
            nonlocal count
            result=await old(*args,**kwargs);count+=1
            if count==3:raise OSError('scan interrupted')
            return result
        with patch.object(self.admission.spool,'native_entry',side_effect=interrupted):
            with self.assertRaises(OSError):await self.prepare()
        async with self.f.master.database.connect() as conn:
            before=await conn.scalar(select(func.count()).select_from(s.plan_paths))
        status=await self.prepare(self.new_admission());self.assertEqual(status['state'],'prepared')
        async with self.f.master.database.connect() as conn:
            after=await conn.scalar(select(func.count()).select_from(s.plan_paths))
        self.assertLess(after,before+30);await self.assert_originals()

    async def test_own_registry_allowance_does_not_ignore_unrelated_work_or_other_binding(self):
        await self.preview();await self.prepare()
        async with self.f.master.database.connect() as conn:
            task=await self.admission._task(conn,self.plan['plan_id'])
            relation=(await conn.execute(select(fs.relationships).where(fs.relationships.c.peer_id==task['source_id']))).mappings().one()
        request=self.admission._request(task,'source');consumer=RegistryClient(self.f.master.store,relation,self.f.transport)
        async def consume(_):pass
        with self.assertRaises(RegistryError):await consumer.read(consume)
        self.assertEqual((await consumer.read(consume,reservation=request))['count'],2)
        with self.assertRaises(p.ProtocolError):await consumer.read(consume,reservation={**request,'total_bytes':request['total_bytes']+1})
        async with self.f.source.database.begin() as conn:await conn.execute(update(fs.storage_members).values(reserved_bytes=50))
        with self.assertRaisesRegex(RegistryError,'pending_or_invalid'):await consumer.read(consume,reservation=request)
        async with self.f.source.database.begin() as conn:await conn.execute(update(fs.storage_members).values(reserved_bytes=0))
        async with self.f.source.database.begin() as conn:
            await conn.execute(insert(s.tasks).values(task_id=uuid.uuid4().hex,master_id=task['master_id'],source_id=task['source_id'],
                target_id=task['target_id'],source_relationship=task['source_relationship'],target_relationship=task['target_relationship'],
                plan_sha256='a'*64,plan={},state='queued',object_count=1,total_bytes=1,created_at=1,updated_at=1))
        with self.assertRaisesRegex(RegistryError,'pending_or_invalid'):await consumer.read(consume,reservation=request)

    async def test_a_second_live_coordinator_cannot_take_admission_lease(self):
        await self.preview();await self.admission._create(self.plan['plan_id'],self.plan['review_sha256'],'test-admin')
        async with self.admission._running(self.plan['plan_id']):
            with self.assertRaisesRegex(AdmissionError,'busy'):await self.prepare(self.new_admission())
        self.assertEqual((await self.prepare(self.new_admission()))['state'],'prepared')

    async def test_expired_coordinator_cannot_change_task_after_lease_takeover(self):
        await self.preview();await self.admission._create(self.plan['plan_id'],self.plan['review_sha256'],'test-admin')
        second=self.new_admission()
        async with self.admission._running(self.plan['plan_id']):
            self.f.now+=46
            async with second._running(self.plan['plan_id']):
                with self.assertRaisesRegex(AdmissionError,'lease_lost'):
                    await self.admission._save(self.plan['plan_id'],phase='stale_write')
                self.assertEqual((await second._save(self.plan['plan_id'],phase='current_runner'))['plan']['phase'],'current_runner')
