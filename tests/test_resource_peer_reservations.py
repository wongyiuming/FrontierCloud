"""Actual SQL/native files: durable preparation, quota and mutation exclusions."""
import asyncio
import copy
import os
import time
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select, update

from app.api import internal_storage_integrity as storage_api
from app.services import resource_fences, resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.federation.state import State
from app.services.resource_peer_reservations import INITIAL_UNITS_SHA256, NativeReservations, ReservationError, unit_transcript
from app.services.resource_observation import digest
from app.core.sqlite_compat import create_sqlite_engine
from tests import test_resource_migration_planning as planning_fixture


@unittest.skipUnless(os.name=='posix','native reservation uses Linux openat')
class PeerReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=planning_fixture.MigrationPlanningTests('runTest')
        await self.f.asyncSetUp()
        for peer in (self.f.source,self.f.target):
            async with peer.database.begin() as conn:
                await conn.execute(insert(fs.storage_members).values(member_id=peer.store.node['node_id'],
                    relationship_id=None,member_kind='Follower',transport='Local',storage_enabled=1,
                    allocated_bytes=8*1024**3,used_bytes=0,reserved_bytes=0,physical_free_bytes=16*1024**3,
                    health='online',writable=1,updated_at=1))
            peer.reservations=NativeReservations(peer.store,root_provider=lambda peer=peer:peer.roots)
        self.peer=self.f.target
        self.units=[self.unit('music/album')]
        self.request=self.reservation()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    def unit(self,parent,*,kind='media',count=1,size=123):
        target=parent if kind=='media' else self.f.target.relationship+'/'+parent.split('/')[1]
        return dict(unit_id=digest([kind,self.f.source.store.node['node_id'],parent]),resource_kind=kind,
            source_parent=parent,target_parent=target,source_version=digest(['native-version',parent]),object_count=count,total_bytes=size)

    def reservation(self,*,units=None,side='target',task=None):
        units=self.units if units is None else units
        transcript=INITIAL_UNITS_SHA256
        for unit in sorted(units,key=lambda value:value['unit_id']):transcript=unit_transcript(transcript,unit)
        return dict(task_id=task or uuid.uuid4().hex,master_id=self.f.master.store.node['node_id'],
            source_id=self.f.source.store.node['node_id'],target_id=self.f.target.store.node['node_id'],
            source_relationship=self.f.source.relationship,target_relationship=self.f.target.relationship,side=side,
            plan_sha256='f'*64,unit_count=len(units),object_count=sum(unit['object_count'] for unit in units),
            total_bytes=sum(unit['total_bytes'] for unit in units),units_sha256=transcript)

    async def relation(self,peer=None):
        peer=self.peer if peer is None else peer
        async with peer.database.connect() as conn:
            return dict((await conn.execute(select(fs.relationships).where(
                fs.relationships.c.relationship_id==peer.relationship))).mappings().one())

    async def call(self,method,*,peer=None,**kwargs):
        peer=self.peer if peer is None else peer
        return await getattr(peer.reservations,method)(peer.store.node,await self.relation(peer),uuid.uuid4().hex,**kwargs)

    async def begin(self,request=None,peer=None):
        return await self.call('begin',peer=peer,reservation=self.request if request is None else request)

    async def quota(self,peer=None):
        peer=self.peer if peer is None else peer
        async with peer.database.connect() as conn:
            return await conn.scalar(select(fs.storage_members.c.reserved_bytes))

    async def ready(self):
        await self.begin()
        for unit in sorted(self.units,key=lambda value:value['unit_id']):
            await self.call('add_unit',task_id=self.request['task_id'],unit=unit)
        return await self.call('seal',task_id=self.request['task_id'])

    async def test_retry_reserves_once_and_changed_request_cannot_replace_it(self):
        first,again=await self.begin(),await self.begin()
        self.assertEqual((first['binding_sha256'],again['binding_sha256']),(digest(self.request),digest(self.request)))
        self.assertEqual(await self.quota(),123)
        changed={**self.request,'total_bytes':124}
        with self.assertRaisesRegex(ReservationError,'request_changed'):await self.begin(changed)
        self.assertEqual(await self.quota(),123)
        self.assertFalse(first['execution_authorized']);self.assertFalse(first['writable_verified'])

    async def test_source_drain_allows_reads_and_needs_no_target_free_space(self):
        _,path,data=await self.f.media(b'ciphertext remains original',encrypted=True)
        request=self.reservation(side='source')
        with patch('app.services.resource_peer_reservations._capacity',return_value=0):
            await self.begin(request,self.f.source)
        async with self.f.source.database.begin() as conn:
            with self.assertRaises(resource_fences.FenceConflict):
                await resource_fences.new_upload(conn,self.f.source.store.node['node_id'],'media','music/new/song.mp3')
        self.assertEqual(path.read_bytes(),data)
        self.assertEqual(await self.quota(self.f.source),0)

    async def test_complete_units_fence_selected_paths_preserving_unrelated_writes(self):
        status=await self.ready()
        self.assertEqual((status['state'],status['unit_count'],status['object_count'],status['total_bytes']),('prepared',1,1,123))
        self.assertEqual(status['units_sha256'],self.request['units_sha256'])
        async with self.peer.database.begin() as conn:
            with self.assertRaises(resource_fences.FenceConflict):
                await resource_fences.new_upload(conn,self.peer.store.node['node_id'],'media','music/album/new.mp3')
            await resource_fences.new_upload(conn,self.peer.store.node['node_id'],'media','music/free/new.mp3')
            with self.assertRaises(resource_fences.FenceConflict):await resource_fences.paths(conn,'media',[('music',True)])
        before=await self.quota()
        again=await self.call('add_unit',task_id=self.request['task_id'],unit=self.units[0])
        self.assertEqual(again['revision'],status['revision']);self.assertEqual(await self.quota(),before)

    async def test_recording_parent_mapping_fences_the_actual_target_user(self):
        user='a'*32
        self.units=[self.unit(self.f.source.relationship+'/'+user,kind='recording')]
        self.request=self.reservation()
        await self.ready()
        async with self.peer.database.begin() as conn:
            with self.assertRaises(resource_fences.FenceConflict):
                await resource_fences.recording(conn,self.peer.store.node['node_id'],user,upload=True)
            await resource_fences.recording(conn,self.peer.store.node['node_id'],'b'*32,upload=True)

    async def test_incomplete_or_wrong_transcript_cannot_be_sealed_or_free_capacity(self):
        self.request['units_sha256']='0'*64
        await self.begin()
        with self.assertRaisesRegex(ReservationError,'incomplete'):await self.call('seal',task_id=self.request['task_id'])
        await self.call('add_unit',task_id=self.request['task_id'],unit=self.units[0])
        with self.assertRaisesRegex(ReservationError,'incomplete'):await self.call('seal',task_id=self.request['task_id'])
        self.assertEqual(await self.quota(),123)

    async def test_wrong_master_role_relationship_or_member_rejected_before_reserving(self):
        for changed in ({**self.request,'master_id':'a'*32},{**self.request,'target_id':'b'*32},
                        {**self.request,'target_relationship':'c'*32}):
            with self.assertRaises(ReservationError):await self.begin(changed)
        relation=await self.relation();relation['credential']='changed'
        with self.assertRaisesRegex(ReservationError,'master_changed'):
            await self.peer.reservations.begin(self.peer.store.node,relation,uuid.uuid4().hex,self.request)
        async with self.peer.database.begin() as conn:await conn.execute(update(fs.identity).values(role='Master'))
        with self.assertRaisesRegex(ReservationError,'master_changed'):await self.begin()
        self.assertEqual(await self.quota(),0)

    async def test_native_capacity_checks_latest_quota_and_physical_reserve(self):
        with patch('app.services.resource_peer_reservations._capacity',return_value=0):
            with self.assertRaisesRegex(ReservationError,'capacity_insufficient'):await self.begin()
        async with self.peer.database.begin() as conn:
            await conn.execute(update(fs.storage_members).values(allocated_bytes=100,used_bytes=0))
        with self.assertRaisesRegex(ReservationError,'capacity_insufficient'):await self.begin()
        self.assertEqual(await self.quota(),0)

    async def test_ordinary_upload_accounts_for_reserved_physical_bytes_and_abort_preserves_it(self):
        await self.begin()
        with patch.object(storage_api,'state',self.peer.store),patch.object(storage_api,'MEDIA_ROOT',self.peer.roots['media']):
            with self.assertRaises(resource_fences.FenceConflict):await storage_api._reserve('e'*64,'music/free/early.mp3',50)
        # Other parents reopen only after every native scope is durable.
        await self.ready()
        with patch.object(storage_api,'state',self.peer.store),patch.object(storage_api,'MEDIA_ROOT',self.peer.roots['media']),\
                patch.object(storage_api.resource_pool,'physical_free',return_value=storage_api.resource_pool.PHYSICAL_RESERVE_BYTES+200):
            with self.assertRaises(HTTPException) as error:await storage_api._reserve('d'*64,'music/free/large.mp3',100)
            self.assertEqual(error.exception.status_code,507)
            await storage_api._reserve('c'*64,'music/free/small.mp3',50)
        self.assertEqual(await self.quota(),173)
        result=await self.call('abort_unstarted',task_id=self.request['task_id'])
        self.assertEqual(result['state'],'aborted');self.assertEqual(await self.quota(),50)
        self.assertEqual((await self.begin())['state'],'aborted')
        self.assertEqual(await self.quota(),50)

    async def test_parallel_prepare_has_one_winner_and_parallel_unit_retry_is_idempotent(self):
        result=await asyncio.gather(self.begin(),self.begin(self.reservation()),return_exceptions=True)
        self.assertEqual(sum(isinstance(value,dict) for value in result),1)
        winner=next(value for value in result if isinstance(value,dict))
        await asyncio.gather(*(self.call('add_unit',task_id=winner['task_id'],unit=self.units[0]) for _ in range(2)))
        self.assertEqual((await self.call('status',task_id=winner['task_id']))['unit_count'],1)
        self.assertEqual(await self.quota(),123)

    async def test_new_store_reloads_preparation_and_old_age_never_releases_it(self):
        await self.ready()
        url=self.peer.database.url
        await self.peer.database.dispose()
        replacement=create_sqlite_engine(url)
        try:
            store=State(replacement,self.peer.vault_key);await store.initialize()
            service=NativeReservations(store,root_provider=lambda:self.peer.roots,wall=lambda:time.time()+10**7)
            result=await service.status(store.node,await self.relation(),uuid.uuid4().hex,self.request['task_id'])
            self.assertEqual(result['state'],'prepared');self.assertEqual(result['capacity_reserved'],123)
            async with replacement.begin() as conn:
                with self.assertRaises(resource_fences.FenceConflict):await resource_fences.node_clear(conn)
        finally:await replacement.dispose()

    async def test_failed_audit_rolls_back_all_preparation_and_unit_metadata(self):
        with patch.object(self.peer.store,'log',side_effect=RuntimeError('audit fault')):
            with self.assertRaisesRegex(RuntimeError,'audit fault'):await self.begin()
        self.assertEqual(await self.quota(),0)
        await self.begin()
        with patch.object(self.peer.store,'log',side_effect=RuntimeError('unit audit fault')):
            with self.assertRaisesRegex(RuntimeError,'unit audit fault'):
                await self.call('add_unit',task_id=self.request['task_id'],unit=self.units[0])
        async with self.peer.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),0)
            self.assertEqual(await conn.scalar(select(s.peer_reservations.c.accepted_units)),0)

    async def test_abort_has_no_file_deletion_and_requires_no_started_transfer(self):
        _,path,data=await self.f.media()
        request=self.reservation(side='source');self.peer=self.f.source
        await self.begin(request)
        await self.call('add_unit',task_id=request['task_id'],unit=self.units[0])
        async with self.peer.database.begin() as conn:
            await conn.execute(insert(s.peer_bindings).values(task_id=request['task_id'],logical_id='a'*64,
                unit_id='c'*64,source_path='music/album/song.mp3',binding_sha256='b'*64,binding_json='{}'))
        with self.assertRaisesRegex(ReservationError,'requires_reconciliation'):
            await self.call('abort_unstarted',task_id=request['task_id'])
        self.assertEqual(path.read_bytes(),data)
        async with self.peer.database.begin() as conn:await conn.execute(delete(s.peer_bindings))
        result=await self.call('abort_unstarted',task_id=request['task_id'])
        self.assertEqual(result['state'],'aborted');self.assertEqual(path.read_bytes(),data)
        async with self.peer.database.begin() as conn:await resource_fences.node_clear(conn)

    async def test_unit_order_overlap_and_cross_user_or_wrong_version_retry_are_refused(self):
        self.units=[self.unit('music/album'),self.unit('music/album/disc')]
        self.request=self.reservation()
        await self.begin()
        ordered=sorted(self.units,key=lambda value:value['unit_id'])
        await self.call('add_unit',task_id=self.request['task_id'],unit=ordered[0])
        with self.assertRaises((ReservationError,resource_fences.FenceConflict)):
            await self.call('add_unit',task_id=self.request['task_id'],unit=ordered[1])
        changed=copy.deepcopy(ordered[0]);changed['source_version']='1'*64
        with self.assertRaisesRegex(ReservationError,'unit_changed'):
            await self.call('add_unit',task_id=self.request['task_id'],unit=changed)

    async def test_pending_native_upload_blocks_prepare_and_cannot_be_reclaimed_by_it(self):
        async with self.peer.database.begin() as conn:
            await conn.execute(update(fs.storage_members).values(reserved_bytes=12))
        with self.assertRaisesRegex(ReservationError,'writer_pending'):await self.begin()
        self.assertEqual(await self.quota(),12)

    async def test_member_authority_blocks_reconfiguration_even_if_separate_flags_are_missing(self):
        await self.begin()
        async with self.peer.database.begin() as conn:
            await conn.execute(delete(s.capacity))
            with self.assertRaises(resource_fences.FenceConflict):
                await resource_fences.member_clear(conn,self.peer.store.node['node_id'])
        with self.assertRaisesRegex(ReservationError,'capacity_inconsistent'):
            await self.call('abort_unstarted',task_id=self.request['task_id'])
        with self.assertRaisesRegex(ReservationError,'capacity_inconsistent'):
            await self.call('status',task_id=self.request['task_id'])
        self.assertEqual(await self.quota(),123)

    async def test_lost_scope_cannot_be_hidden_by_seal_or_a_unit_retry(self):
        await self.begin()
        await self.call('add_unit',task_id=self.request['task_id'],unit=self.units[0])
        async with self.peer.database.begin() as conn:await conn.execute(delete(s.scopes))
        for action,extra in (('seal',{}),('add_unit',dict(unit=self.units[0]))):
            with self.assertRaisesRegex(ReservationError,'scope_inconsistent'):
                await self.call(action,task_id=self.request['task_id'],**extra)
        self.assertEqual(await self.quota(),123)
