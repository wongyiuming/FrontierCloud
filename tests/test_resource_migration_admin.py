"""Admin ASGI inputs over real SQL/native migration services; not TLS proof."""
import asyncio
import os
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI,HTTPException
import httpx
from sqlalchemy import func,insert,select,update

from app.api.v1 import admin_migrations as api
from app.api.v1.admin import require_session
from app.services import resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.resource_migration_runtime import Runtime
from tests import test_resource_scheduler as fixtures


@unittest.skipUnless(os.name=='posix','migration Admin observes native Linux files')
class MigrationAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.SchedulerTests('runTest');await self.fixture.asyncSetUp();self.f=self.fixture.f;self.fenced=False
        self.runtime=Runtime(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now,fenced=lambda:self.fenced)
        self.runtime.planner=self.f.planner
        self.runtime.scheduler.admission.preview_epoch=self.f.planner.epoch
        self.runtime.scheduler.transfer.preparation.preview_epoch=self.f.planner.epoch
        self.patching=patch.object(api,'runtime',self.runtime);self.patching.start()
        self.app=FastAPI();self.app.include_router(api.router,prefix='/api/v1/media/admin')
        self.app.dependency_overrides[require_session]=lambda:'test-admin'
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app,client=('198.51.100.9',5421)),base_url='https://master.4399.com')
        self.base='/api/v1/media/admin/migrations'

    async def asyncTearDown(self):
        try:await self.http.aclose();await self.runtime.stop()
        finally:self.patching.stop();await self.fixture.asyncTearDown()

    async def preview(self):
        self.originals=[await self.f.media(b'plain-original'*200),
            await self.f.media(b'encrypted-original'*300,path='music/second/private.mp3',encrypted=True)]
        self.ident=uuid.uuid4().hex
        response=await self.http.post(self.base+'/plans',json=dict(request_id=self.ident,
            source_id=self.f.source.store.node['node_id'],target_id=self.f.target.store.node['node_id']))
        self.assertEqual(response.status_code,200,response.text)
        await self.runtime.planner.task
        response=await self.http.get(self.base+'/plans/'+self.ident);self.assertEqual(response.status_code,200,response.text)
        self.plan=response.json();self.assertEqual(self.plan['state'],'ready',self.plan)
        self.approval=dict(approved_sha256=self.plan['review_sha256'],confirm_source_id=self.f.source.store.node['node_id'],
            confirm_target_id=self.f.target.store.node['node_id'])
        return self.plan

    async def start(self):
        response=await self.http.post(self.base+'/plans/'+self.ident+'/start',json=self.approval)
        self.assertEqual(response.status_code,200,response.text);return response.json()

    async def status(self):
        response=await self.http.get(self.base+'/tasks/'+self.ident);self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store');return response.json()

    async def reach(self,state):
        for _ in range(250):
            await self.runtime.scheduler.tick();row=await self.status()
            self.assertNotEqual(row['state'],'failed',row)
            if row['state']==state:return row
            await asyncio.sleep(0)
        self.fail('Admin task did not reach '+state)

    async def action(self,row,action):
        return await self.http.post(self.base+'/tasks/'+self.ident,json=dict(revision=row['revision'],action=action))

    async def test_readonly_preview_then_durable_start_replay_and_real_per_parent_cutover(self):
        await self.preview()
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.tasks)),0)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.capacity)),0)
        first=await self.start();second=await self.start();self.assertEqual(first,second)
        self.assertEqual(first['state'],'admitting');self.assertEqual(first['committed_bytes'],0)
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),self.plan['total_bytes'])
        task=await self.reach('cleanup_pending');self.assertEqual(task['committed_bytes'],self.plan['total_bytes'])
        self.assertEqual({u['state'] for u in task['units']},{'committed'})
        self.assertNotIn('credential',str(task));self.assertNotIn('private_key',str(task));self.assertNotIn(str(self.f.root),str(task))
        for _,path,data in self.originals:
            self.assertEqual(path.read_bytes(),data)
            self.assertEqual((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).read_bytes(),data)
        history=await self.http.get(self.base+'/tasks');self.assertEqual(history.json()['items'][0]['task_id'],self.ident)

    async def test_manual_pause_survives_scheduler_reconstruction_and_requires_explicit_resume(self):
        await self.preview();await self.start();task=await self.reach('copying')
        response=await self.action(task,'pause');self.assertEqual(response.status_code,200,response.text)
        self.assertEqual((response.json()['state'],response.json()['pause_reason']),('paused','manual'))
        await self.runtime.scheduler.stop()
        from app.services.resource_migration_scheduler import Scheduler
        self.runtime.scheduler=Scheduler(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now,fenced=lambda:False)
        await self.runtime.scheduler.tick();task=await self.status();self.assertEqual(task['pause_reason'],'manual')
        self.assertEqual((await self.action(task,'resume')).status_code,200)
        await self.reach('cleanup_pending')

    async def test_admission_cancel_is_durable_before_peer_work_and_refunds_only_after_reconciliation(self):
        await self.preview();task=await self.start()
        response=await self.action(task,'cancel');self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'admission_aborting')
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),self.plan['total_bytes'])
        await self.reach('cancelled')
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.capacity)),0)
        for _,path,data in self.originals:self.assertEqual(path.read_bytes(),data)

    async def test_wrong_confirmation_stale_revision_and_busy_admission_cannot_release_capacity(self):
        await self.preview();bad={**self.approval,'confirm_target_id':'f'*32}
        response=await self.http.post(self.base+'/plans/'+self.ident+'/start',json=bad)
        self.assertEqual(response.status_code,409);self.assertEqual(response.json()['code'],'migration_admin_confirmation_mismatch')
        task=await self.start()
        response=await self.action({**task,'revision':task['revision']+1},'cancel');self.assertEqual(response.status_code,409)
        async with self.runtime.scheduler.admission._running(self.ident):
            response=await self.action(task,'cancel');self.assertEqual(response.status_code,409)
            self.assertEqual(response.json()['code'],'migration_admission_busy')
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),self.plan['total_bytes'])

    async def test_invalid_json_types_unknown_fields_and_oversized_body_never_create_a_task(self):
        await self.preview()
        path=self.base+'/plans/'+self.ident+'/start'
        for value in ({**self.approval,'force':True},{**self.approval,'confirm_target_id':123}):
            self.assertEqual((await self.http.post(path,json=value)).status_code,400)
        raw=b'{"revision":1,"revision":2,"action":"cancel"}'
        self.assertEqual((await self.http.post(self.base+'/tasks/'+self.ident,content=raw)).status_code,400)
        self.assertEqual((await self.http.post(path,content=b'x'*4097)).status_code,413)
        for revision in (True,1.0,'1'):
            self.assertEqual((await self.http.post(self.base+'/tasks/'+self.ident,json=dict(revision=revision,action='cancel'))).status_code,400)
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.tasks)),0)

    async def test_all_routes_require_session_and_secure_transport_before_domain_actions(self):
        async def denied():raise HTTPException(401,'Admin session required')
        self.app.dependency_overrides[require_session]=denied
        paths=('/members','/tasks','/plans/'+'a'*32,'/tasks/'+'a'*32)
        for path in paths:self.assertEqual((await self.http.get(self.base+path)).status_code,401)
        self.assertEqual((await self.http.post(self.base+'/plans',content=b'x'*4097)).status_code,401)
        self.app.dependency_overrides[require_session]=lambda:'test-admin'
        self.assertEqual((await self.http.get('http://master.4399.com'+self.base+'/tasks')).status_code,426)
        self.assertEqual((await self.http.get(self.base+'/tasks?after=bad')).status_code,409)

    async def test_maintenance_and_non_master_identity_refuse_mutations_without_lying_about_state(self):
        await self.preview();self.fenced=True
        response=await self.http.post(self.base+'/plans/'+self.ident+'/start',json=self.approval)
        self.assertEqual(response.status_code,409);self.assertEqual(response.json()['code'],'migration_admin_maintenance_fenced')
        self.fenced=False
        async with self.f.master.database.begin() as conn:await conn.execute(update(fs.identity).values(role='Follower'))
        for path in ('/members','/tasks'):
            response=await self.http.get(self.base+path);self.assertEqual(response.status_code,409)
            self.assertEqual(response.json()['code'],'migration_admin_master_required')

    async def test_member_pages_cover_the_complete_pool_without_credentials_or_implicit_truncation(self):
        async with self.f.master.database.begin() as conn:
            relationship=dict((await conn.execute(select(fs.relationships).where(fs.relationships.c.peer_id==self.f.source.store.node['node_id']))).mappings().one())
            member=dict((await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==self.f.source.store.node['node_id']))).mappings().one())
            for index in range(65):
                peer_id=f'{1000+index:032x}';relation_id=f'{2000+index:032x}'
                await conn.execute(insert(fs.relationships).values(**{**relationship,'relationship_id':relation_id,'peer_id':peer_id}))
                await conn.execute(insert(fs.storage_members).values(**{**member,'member_id':peer_id,'relationship_id':relation_id}))
        first=(await self.http.get(self.base+'/members')).json()
        self.assertEqual(len(first['items']),64);self.assertFalse(first['complete']);self.assertTrue(first['next_cursor'])
        second=(await self.http.get(self.base+'/members?after='+first['next_cursor'])).json()
        self.assertEqual(len(second['items']),3);self.assertTrue(second['complete']);self.assertEqual(second['next_cursor'],'')
        self.assertEqual(len({r['member_id'] for r in first['items']+second['items']}),67)
        self.assertNotIn('credential',str(first)+str(second));self.assertNotIn('peer_key',str(first)+str(second))
        self.assertEqual((await self.http.get(self.base+'/members?after=invalid')).status_code,409)


if __name__=='__main__':unittest.main()
