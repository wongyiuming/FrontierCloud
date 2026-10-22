"""Owned active-transfer aborts with real files, quota, audit and crash boundaries."""
import asyncio
import json
import os
import threading
import unittest
import uuid
from unittest.mock import patch

import httpx
from anyio import CancelScope
from sqlalchemy import func,insert,select,update

from app.services import resource_migration_schema as s,resource_transfer_files as files
from app.services.federation import protocol as p,schema as fs
from app.services.resource_transfer_cancel import CancelPeers,Cancellation
from app.services.resource_transfer_protocol import TransferError,target_request
from tests import test_resource_transfer as fixtures


@unittest.skipUnless(os.name=='posix','managed abort requires Linux nofollow paths')
class TransferCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.ResourceTransferTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f;self.original=self.f.transport.request;self.after=None;self.before=None
        self.f.transport.request=self.control
        self.cancel=Cancellation(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def asyncTearDown(self):await self.fixture.asyncTearDown()

    async def control(self,origin,path,**kwargs):
        if '/transfer/abort-' not in path:return await self.original(origin,path,**kwargs)
        peer=next(peer for peer in (self.f.source,self.f.target) if peer.store.node['endpoint']==origin)
        self.assertEqual((kwargs['credential'],kwargs['relation']),(peer.credential,peer.relationship))
        async with peer.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==peer.relationship))).mappings().one())
        if self.before:await self.before(peer,path,kwargs['value'])
        result=await getattr(CancelPeers(peer.transfers),path.rsplit('-',1)[-1])(peer.store.node,relation,**kwargs['value'])
        if self.after:await self.after(peer,path,kwargs['value'],result)
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)

    async def prepare(self,*,partial=True):
        await self.fixture.prepare(large=partial);await self.fixture.queue()
        self.first=await self.fixture.step();self.path=self.fixture.stage()
        await self.cancel.request(self.fixture.plan['plan_id'],self.fixture.lease.revision,'test-admin')

    async def finish(self,*,service=None):
        for _ in range(12):
            result=await (service or self.cancel).advance(self.fixture.plan['plan_id'],'test-admin')
            if result['state']=='cancelled':return result
        self.fail('abort did not converge')

    async def assert_original(self):
        self.assertEqual(self.fixture.original.read_bytes(),self.fixture.data)
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.global_media.c.storage_member_id)),self.f.source.store.node['node_id'])
            self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),0)

    async def test_partial_cancel_cleans_only_owned_stage_and_settles_all_reservations(self):
        await self.prepare();self.assertTrue(self.path.exists())
        await self.finish();self.assertFalse(self.path.exists())
        self.assertEqual(await self.fixture.fixture.quota(),(0,0))
        for peer in (self.f.master,self.f.source,self.f.target):
            async with peer.database.connect() as conn:
                for table in (s.scopes,s.capacity,s.drains):self.assertEqual(await conn.scalar(select(func.count()).select_from(table)),0)
        await self.assert_original()
        self.assertEqual((await self.cancel.advance(self.fixture.plan['plan_id'],'test-admin'))['state'],'cancelled')

    async def test_verified_but_unpublished_copy_can_be_cancelled(self):
        await self.prepare(partial=False);self.assertEqual(self.first['state'],'verified')
        await self.finish();self.assertFalse(self.path.exists());await self.assert_original()

    async def test_stop_waits_for_unstarted_live_stream_then_rejects_old_grants(self):
        await self.fixture.prepare(large=True);await self.fixture.queue();await self.fixture.step()
        self.path=self.fixture.stage();grant=self.fixture.grants[-1]
        request=target_request(self.f.target.store.unseal(self.f.target.store.node['private_key']),grant,offset=0,length=1024,now=self.f.now)
        _,handle=await self.f.source.transfers.source(request)
        await self.cancel.request(self.fixture.plan['plan_id'],self.fixture.lease.revision,'test-admin')
        result=await self.cancel.advance(self.fixture.plan['plan_id'],'test-admin')
        self.assertEqual(result['peers']['source'],'waiting_writer');self.assertTrue(self.path.exists())
        with self.assertRaises(TransferError):await anext(handle.body)
        await handle.close();await self.finish();await self.assert_original()
        request=target_request(self.f.target.store.unseal(self.f.target.store.node['private_key']),grant,offset=0,length=1024,now=self.f.now)
        with self.assertRaises(p.ProtocolError):await self.f.source.transfers.source(request)

    async def test_stop_never_unlinks_a_joined_write_still_in_progress(self):
        await self.fixture.prepare(large=True);await self.fixture.queue()
        started,release=threading.Event(),threading.Event();original=self.f.target.transfers._write
        def blocked(*args):
            original(*args);started.set()
            if not release.wait(5):raise OSError('fixture writer not released')
        job=None
        try:
            with patch.object(self.f.target.transfers,'_write',side_effect=blocked):
                job=asyncio.create_task(self.fixture.step())
                async with asyncio.timeout(5):
                    while not started.is_set():await asyncio.sleep(.01)
                self.path=self.fixture.stage()
                await self.cancel.request(self.fixture.plan['plan_id'],self.fixture.lease.revision,'test-admin')
                result=await self.cancel.advance(self.fixture.plan['plan_id'],'test-admin')
                self.assertEqual(result['peers']['target'],'waiting_writer')
                self.assertTrue(self.path.exists());self.assertFalse(job.done())
                self.assertEqual(await self.fixture.fixture.quota(),(len(self.fixture.data),)*2)
                release.set()
                with self.assertRaises(p.ProtocolError):await job
            await self.finish();await self.assert_original()
        finally:
            release.set()
            if job is not None and not job.done():await asyncio.gather(job,return_exceptions=True)

    async def test_cancel_scope_cannot_interrupt_reader_database_cleanup(self):
        await self.fixture.prepare(large=True);await self.fixture.queue();await self.fixture.step()
        grant=self.fixture.grants[-1]
        request=target_request(self.f.target.store.unseal(self.f.target.store.node['private_key']),grant,offset=0,length=1024,now=self.f.now)
        _,handle=await self.f.source.transfers.source(request)
        with CancelScope() as scope:
            scope.cancel();await handle.close()
        self.assertFalse(self.f.source.transfers.source_lock.locked())
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.read_leases)),0)

    async def test_unknown_file_retains_fences_quota_and_source_data(self):
        await self.prepare();unknown=self.path.parent/'unknown-user-file';unknown.write_bytes(b'never delete me')
        with self.assertRaisesRegex(TransferError,'unknown_stage'):await self.finish()
        self.assertEqual(unknown.read_bytes(),b'never delete me')
        self.assertEqual(await self.fixture.fixture.quota(),(len(self.fixture.data),)*2)
        await self.assert_original()

    async def test_replaced_stage_inode_is_refused_without_deleting_replacement(self):
        await self.prepare();saved=self.path.with_name('preserved-original-part');self.path.rename(saved)
        self.path.write_bytes(b'unrecognized replacement')
        self.path.chmod(0o600)
        with self.assertRaisesRegex(TransferError,'stage_changed'):await self.finish()
        self.assertEqual(self.path.read_bytes(),b'unrecognized replacement');self.assertTrue(saved.exists())
        self.assertEqual(await self.fixture.fixture.quota(),(len(self.fixture.data),)*2)

    async def test_unlink_then_lost_audit_ack_retries_absence_without_releasing_quota_early(self):
        await self.prepare();log=self.f.target.store.log;failed=False
        async def fault(conn,action,*args,**kwargs):
            nonlocal failed
            if action=='migration_stage_aborted' and not failed:failed=True;raise OSError('audit failure after unlink')
            return await log(conn,action,*args,**kwargs)
        with patch.object(self.f.target.store,'log',side_effect=fault):
            with self.assertRaises(OSError):await self.cancel.advance(self.fixture.plan['plan_id'],'test-admin')
        self.assertFalse(self.path.exists());self.assertEqual(await self.fixture.fixture.quota(),(len(self.fixture.data),)*2)
        replacement=Cancellation(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        await self.finish(service=replacement);self.assertEqual(await self.fixture.fixture.quota(),(0,0))

    async def test_recreated_file_after_unlink_intent_is_retained_on_retry(self):
        await self.prepare();original=files.unlink_stage;failed=False
        def fault(*args):
            nonlocal failed
            original(*args)
            if not failed:failed=True;raise OSError('lost filesystem acknowledgement')
        with patch.object(files,'unlink_stage',side_effect=fault):
            with self.assertRaises(OSError):await self.cancel.advance(self.fixture.plan['plan_id'],'test-admin')
        self.path.write_bytes(b'new user file')
        self.path.chmod(0o600)
        with self.assertRaisesRegex(TransferError,'stage_changed'):await self.cancel.advance(self.fixture.plan['plan_id'],'test-admin')
        self.assertEqual(self.path.read_bytes(),b'new user file')

    async def test_lost_terminal_peer_reply_keeps_master_reserved_until_verified_retry(self):
        await self.prepare();failed=False
        async def after(peer,path,value,result):
            nonlocal failed
            if peer is self.f.target and path.endswith('abort-clean') and result['state']=='aborted' and not failed:
                failed=True;raise httpx.ReadError('lost terminal reply')
        self.after=after
        with self.assertRaises(httpx.ReadError):await self.finish()
        self.assertEqual(await self.fixture.fixture.quota(),(len(self.fixture.data),0))
        self.after=None;await self.finish();self.assertEqual(await self.fixture.fixture.quota(),(0,0))

    async def test_final_master_audit_failure_rolls_back_only_its_settlement(self):
        await self.prepare();log=self.f.master.store.log
        async def fault(conn,action,*args,**kwargs):
            if action=='migration_transfer_cancelled':raise OSError('final audit failed')
            return await log(conn,action,*args,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fault):
            with self.assertRaises(OSError):await self.finish()
        self.assertEqual(await self.fixture.fixture.quota(),(len(self.fixture.data),0))
        await self.finish();self.assertEqual(await self.fixture.fixture.quota(),(0,0));await self.assert_original()

    async def test_unrelated_reserved_capacity_and_playback_readers_are_preserved(self):
        await self.prepare()
        for peer in (self.f.master,self.f.target):
            async with peer.database.begin() as conn:await conn.execute(update(fs.storage_members).where(
                fs.storage_members.c.member_id==self.f.target.store.node['node_id']).values(reserved_bytes=fs.storage_members.c.reserved_bytes+50))
        async with self.f.source.database.begin() as conn:
            await conn.execute(insert(s.read_leases).values(lease_id='d'*32,member_id=self.f.source.store.node['node_id'],
                logical_id=self.fixture.logical,object_id='e'*64,source_version='f'*64,expires_at=int(self.f.now)+100))
        await self.finish();self.assertEqual(await self.fixture.fixture.quota(),(50,50))
        async with self.f.source.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.read_leases)),1)

    async def test_high_traffic_stops_authority_but_defers_heavy_cleanup(self):
        await self.prepare();original=self.path.read_bytes()
        with patch.object(self.f.traffic,'snapshot',return_value={'can_run':False}):
            result=await self.cancel.advance(self.fixture.plan['plan_id'],'test-admin')
        self.assertEqual(result['peers']['target'],'waiting_low_traffic');self.assertEqual(self.path.read_bytes(),original)
        await self.finish()

    async def test_failed_probe_marker_is_cleaned_without_touching_source_or_unknown_data(self):
        await self.fixture.prepare();original=files.write
        def failed(*args):original(*args);raise OSError('probe interrupted')
        with patch.object(files,'write',side_effect=failed):
            with self.assertRaises(OSError):await self.fixture.master.preflight(self.fixture.plan['plan_id'],'test-admin')
        async with self.f.master.database.connect() as conn:revision=await conn.scalar(select(s.tasks.c.revision))
        await self.cancel.request(self.fixture.plan['plan_id'],revision,'test-admin')
        await self.finish();self.assertFalse(list(self.f.target.roots['media'].rglob('probe.bin')))
        self.assertEqual(await self.fixture.fixture.quota(),(0,0));await self.assert_original()

    async def test_publishing_unit_refuses_cancel_and_preserves_every_file(self):
        await self.prepare();raw=self.path.read_bytes()
        async with self.f.target.database.begin() as conn:await conn.execute(update(s.units).values(state='publishing'))
        with self.assertRaisesRegex(TransferError,'published_material'):await self.cancel.advance(self.fixture.plan['plan_id'],'test-admin')
        self.assertEqual(self.path.read_bytes(),raw);await self.assert_original()
