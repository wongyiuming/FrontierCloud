"""Actual SQLite mutation guards, preserved files and unchanged heartbeat state."""
import os
import time
import unittest
from unittest.mock import patch

from sqlalchemy import insert, select, update

from app.api import internal_storage_integrity as storage_api
from app.services import karaoke_delete_integrity as recording_delete, karaoke_schema as ks
from app.services import resource_fences as guards, resource_migration_schema as s
from app.services import resource_pool, storage_capacity
from app.services import media_directories
from app.services.federation import schema as fs
from app.services.resource_observation import digest
from tests import test_resource_migration_planning as fixture


@unittest.skipUnless(os.name == 'posix','resource mutations use native Linux fixtures')
class ResourceFenceTests(unittest.IsolatedAsyncioTestCase):
    request,identity,media = (getattr(fixture.MigrationPlanningTests,name) for name in ('request','identity','media'))

    async def asyncSetUp(self):
        await fixture.MigrationPlanningTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await fixture.MigrationPlanningTests.asyncTearDown(self)

    async def scope(self,*,peer=None,member=None,path='music/album',kind='media'):
        peer = self.master if peer is None else peer
        member = self.source.store.node['node_id'] if member is None else member
        async with peer.database.begin() as conn:
            await conn.execute(insert(s.scopes).values(scope_id=digest([kind,member,path]),task_id='a'*32,
                member_id=member,resource_kind=kind,parent_path=path,created_at=1))

    async def drain(self,*,peer=None):
        peer = self.master if peer is None else peer
        async with peer.database.begin() as conn:
            await conn.execute(insert(s.drains).values(member_id=self.source.store.node['node_id'],
                owner_id='a'*32,reason='migration',created_at=1))

    async def test_drain_stops_placement_without_inventing_offline_or_disabling_reads(self):
        await self.drain()
        summary = await resource_pool.pool_summary(self.master.database)
        row = next(value for value in summary['members'] if value['member_id'] == self.source.store.node['node_id'])
        self.assertEqual((row['available_bytes'],row['health'],row['writable'],row['draining']),(0,'online',1,True))
        shown = await storage_capacity.enrich_pool_summary(summary,self.master.store)
        self.assertEqual(next(value['available_bytes'] for value in shown['members'] if value['member_id'] == row['member_id']),0)
        async with self.master.database.begin() as conn:
            with self.assertRaises(guards.FenceConflict) as error:
                await guards.new_upload(conn,row['member_id'],'media','music/new/song.mp3')
            self.assertEqual(error.exception.code,'migration_member_draining')
        _,native,data = await self.media()
        self.assertEqual(native.read_bytes(),data)

    async def test_file_parent_scopes_cross_members_but_not_sibling_or_nested_units(self):
        await self.scope(member=self.target.store.node['node_id'])
        async with self.master.database.begin() as conn:
            with self.assertRaises(guards.FenceConflict):
                await guards.new_upload(conn,self.source.store.node['node_id'],'media','music/album/new.mp3')
            for path in ('music/album-two/new.mp3','music/album/disc/new.mp3'):
                await guards.new_upload(conn,self.source.store.node['node_id'],'media',path)
            with self.assertRaises(guards.FenceConflict):
                await guards.paths(conn,'media',[('music',True)])
            await guards.paths(conn,'media',[('music/album/disc',True)])

    async def test_master_delete_checks_scope_before_marking_any_selected_rows(self):
        first,native,data = await self.media()
        second,_,_ = await self.media(path='music/free/song.mp3')
        await self.scope()
        with self.assertRaises(guards.FenceConflict):
            await resource_pool.mark_pending_delete([first,second],self.master.database)
        async with self.master.database.connect() as conn:
            self.assertEqual(set((await conn.execute(select(fs.global_media.c.state))).scalars()),{'active'})
        self.assertEqual(native.read_bytes(),data)

    async def test_native_reserve_and_reconcile_do_not_touch_scoped_files(self):
        logical,native,data = await self.media()
        async with self.master.database.connect() as conn:
            object_id = await conn.scalar(select(fs.global_media.c.object_id).where(fs.global_media.c.media_id == logical))
        await self.scope(peer=self.source)
        with patch.object(storage_api,'state',self.source.store),patch.object(storage_api,'MEDIA_ROOT',self.source.roots['media']):
            with self.assertRaises(guards.FenceConflict):
                await storage_api._reserve('b'*64,'music/album/new.mp3',3)
            with self.assertRaises(guards.FenceConflict):
                await storage_api._reconcile_unlocked(object_id,'music/album/song.mp3',len(data))
        self.assertEqual(native.read_bytes(),data)

    async def test_native_and_master_directory_rename_refuse_scope_before_moving_bytes(self):
        _,native,data = await self.media()
        await self.scope(peer=self.source)
        with patch.object(media_directories,'MEDIA_ROOT',self.source.roots['media']):
            with self.assertRaises(guards.FenceConflict):
                await media_directories.rename_follower_directory('music/album','music/new',self.source.database)
        await self.scope()
        with patch.object(media_directories,'node_state',self.master.store):
            with self.assertRaises(guards.FenceConflict):
                await media_directories._preflight_master('music/album','music/new')
        self.assertEqual(native.read_bytes(),data)
        self.assertFalse((native.parent.parent/'new').exists())

    async def test_quota_configuration_cannot_discard_live_reserved_capacity(self):
        member = self.source.store.node['node_id']
        async with self.master.database.begin() as conn:
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id == member).values(reserved_bytes=4*1024**3))
        with self.assertRaisesRegex(ValueError,'used and reserved'):
            await resource_pool.configure_member(member,storage_enabled=True,allocated_bytes=2*1024**3,
                backup_enabled=False,actor='test',store=self.master.store)
        async with self.master.database.connect() as conn:
            row = (await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id == member))).mappings().one()
        self.assertEqual((row['reserved_bytes'],row['allocated_bytes']),(4*1024**3,8*1024**3))

    async def test_mode_config_revocation_and_master_reset_preserve_busy_state(self):
        await self.drain()
        member = self.source.store.node['node_id']
        master_id = self.master.store.node['node_id']
        for operation in (
            self.master.store.set_mode(self.source.relationship,'Relay','test'),
            self.master.store.revoke(self.source.relationship,'test'),
            self.master.store.reset('test',master_id),
            resource_pool.configure_member(member,storage_enabled=False,allocated_bytes=8*1024**3,
                backup_enabled=False,actor='test',store=self.master.store),
        ):
            with self.assertRaises(guards.FenceConflict):
                await operation
        async with self.master.database.connect() as conn:
            row = (await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id == self.source.relationship))).mappings().one()
            actual = await conn.scalar(select(fs.identity.c.node_id))
        self.assertEqual((row['state'],row['mode'],actual),('active','Direct',master_id))

    async def test_same_follower_heartbeat_configuration_preserves_reservations_and_drain(self):
        config = dict(storage=dict(enabled=True,allocated_bytes=8*1024**3),
            compute=dict(enabled=False,worker_slots=0),backup=dict(enabled=False))
        await resource_pool.accept_follower_configuration(config,self.source.store.node,self.source.database)
        async with self.source.database.begin() as conn:
            await conn.execute(update(fs.storage_members).values(reserved_bytes=123))
        await self.drain(peer=self.source)
        await resource_pool.accept_follower_configuration(config,self.source.store.node,self.source.database)
        async with self.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.reserved_bytes)),123)
        config['storage']['enabled'] = False
        with self.assertRaises(guards.FenceConflict):
            await resource_pool.accept_follower_configuration(config,self.source.store.node,self.source.database)

    async def test_recording_scope_is_member_user_specific_and_preserves_account_rows(self):
        user,recording = 'c'*32,'d'*32
        member = self.source.store.node['node_id']
        await self.scope(kind='recording',path=self.source.relationship+'/'+user)
        async with self.master.database.begin() as conn:
            await conn.execute(insert(ks.users).values(user_id=user,username='original',username_key='original',
                password_hash='preserved',status='active',quota_bytes=10000,used_bytes=3,created_at=1,updated_at=1))
            await conn.execute(insert(ks.recordings).values(recording_id=recording,user_id=user,storage_member_id=member,
                filename='original.webm',content_type='audio/webm',size_bytes=3,sha256='a'*64,state='ready',
                title='original',lyrics=[],created_at=1,updated_at=1))
            await guards.recording(conn,self.target.store.node['node_id'],user,upload=True)
            await guards.recording(conn,member,'f'*32,upload=True)
        with patch.object(recording_delete,'state',self.master.store):
            with self.assertRaises(guards.FenceConflict):
                await recording_delete.stage_recording(user,recording,pending=False)
            with self.assertRaises(guards.FenceConflict):
                await recording_delete.stage_user_deletion(user)
        async with self.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(ks.users.c.status)),'active')
            self.assertEqual(await conn.scalar(select(ks.recordings.c.state)),'ready')

    async def test_reader_and_failed_task_guards_are_not_cleared_by_age_or_restart(self):
        member = self.source.store.node['node_id']
        async with self.master.database.begin() as conn:
            await conn.execute(insert(s.read_leases).values(lease_id='a'*32,member_id=member,logical_id='a'*64,
                object_id='b'*64,source_version='c'*64,expires_at=int(time.time())+60))
            await guards.member_clear(conn,member)
            with self.assertRaises(guards.FenceConflict):
                await guards.member_clear(conn,member,readers=True)
            await conn.execute(update(s.read_leases).values(expires_at=1))
            await guards.member_clear(conn,member,readers=True)
            await conn.execute(insert(s.tasks).values(task_id='a'*32,master_id=self.master.store.node['node_id'],
                source_id=member,target_id=self.target.store.node['node_id'],source_relationship=self.source.relationship,
                target_relationship=self.target.relationship,plan_sha256='a'*64,plan={},state='failed',object_count=0,
                total_bytes=0,created_at=1,updated_at=1))
            with self.assertRaises(guards.FenceConflict):
                await guards.member_clear(conn,member)
            await conn.execute(update(s.tasks).values(state='cancelled'))
            await guards.member_clear(conn,member)
        await self.scope()
        async with self.master.database.begin() as conn:
            with self.assertRaises(guards.FenceConflict):
                await guards.member_clear(conn,member)
