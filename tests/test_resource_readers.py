"""Real native files and separate SQL stores; no TLS or payload-delivery claim."""
import asyncio
import os
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

from fastapi import FastAPI,HTTPException
from fastapi.responses import Response
import httpx
from sqlalchemy import func,insert,select,text,update

from app.api import internal_nodes
from app.services import karaoke_storage,media_crypto,media_manager,resource_readers as readers
from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from tests import test_resource_peer_reservations as fixtures


@unittest.skipUnless(os.name=='posix','read leases require native Linux descriptors')
class ReaderLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.PeerReservationTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f;self.peer=self.f.source
        self.logical,self.path,self.data=await self.f.media(b'opaque original bytes',encrypted=True)
        self.object_id=await self.object_id_for(self.path)
        self.relation=await self.fixture.relation(self.peer)
        self.expires=int(time.time())+p.TOKEN_SECONDS
        self.patches=[patch.object(media_manager,'MEDIA_ROOT',self.peer.roots['media']),
            patch.object(karaoke_storage,'ROOT',self.peer.roots['recordings']),
            patch.object(karaoke_storage,'state',self.peer.store),patch.object(internal_nodes,'state',self.peer.store),
            patch.object(internal_nodes.settings,'TLS_ENABLED',True)]
        for item in self.patches:item.start()
        app=FastAPI();app.include_router(internal_nodes.router)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=self.peer.store.node['endpoint'])

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):item.stop()
        await self.fixture.asyncTearDown()

    async def object_id_for(self,path):
        async with self.peer.database.connect() as conn:
            return await conn.scalar(text('SELECT media_id FROM media_objects WHERE media_path=:path'),
                dict(path=path.relative_to(self.peer.roots['media']).as_posix()))

    async def begin(self):
        return await readers.media(self.peer.store,self.relation,self.object_id,self.logical,
            self.path.relative_to(self.peer.roots['media']).as_posix(),self.expires)

    async def rows(self):
        async with self.peer.database.connect() as conn:
            return [dict(row) for row in (await conn.execute(select(s.read_leases))).mappings()]

    async def cleanup_intent(self):
        unit=self.fixture.units[0];request=self.fixture.reservation(side='source')
        await self.fixture.begin(request,self.peer)
        await self.fixture.call('add_unit',peer=self.peer,task_id=request['task_id'],unit=unit)
        await self.fixture.call('seal',peer=self.peer,task_id=request['task_id'])
        async with self.peer.database.begin() as conn:
            await conn.execute(update(s.peer_reservations).values(state='active'))
            await conn.execute(update(s.units).values(state='cleanup_pending'))
        return request['task_id']

    async def test_authenticated_get_and_head_keep_durable_leases_after_asgi_returns(self):
        token=p.media_token(self.peer.credential,self.peer.relationship,self.f.master.store.node['node_id'],
            self.peer.store.node['node_id'],self.object_id,self.logical,int(time.time()))
        stream=AsyncMock(return_value=Response(headers={'X-Accel-Redirect':'/_protected_media/music/album/song.mp3'}))
        with (patch('app.api.v1.media.stream_media_file',stream),
                patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted media')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read payload'))):
            for method in ('GET','HEAD'):
                response=await self.client.request(method,'/internal/v1/media/'+self.object_id,
                    headers={'X-Media-Capability':token,'Origin':self.relation['peer_endpoint']})
                self.assertEqual(response.status_code,200,response.text)
            preflight=await self.client.options('/internal/v1/media/'+self.object_id,params={'token':token})
            missing=await self.client.get('/internal/v1/media/'+self.object_id)
            unpaired=await self.client.get('/internal/v1/media/'+self.object_id,params={'token':token},
                headers={'Origin':'https://unpaired.example.com'})
        self.assertEqual((preflight.status_code,missing.status_code,unpaired.status_code),(200,401,403))
        rows=await self.rows();self.assertEqual(len(rows),2)
        for row in rows:
            self.assertEqual((row['purpose'],row['task_id'],row['logical_id'],row['object_id']),
                ('playback','',self.logical,self.object_id))
            self.assertEqual(row['expires_at'],p.verify_media_token(self.peer.credential,token,int(time.time()))['e']+5)
            self.assertEqual(len(row['source_version']),64)
        self.assertEqual(self.path.read_bytes(),self.data)

    async def test_recording_stream_and_download_protect_the_native_relationship_namespace(self):
        user,recording='a'*32,'b'*32
        path=self.peer.roots['recordings']/self.peer.relationship/user/(recording+'.bin')
        path.parent.mkdir(parents=True);path.write_bytes(b'opaque recording and footer')
        for operation in ('stream','download'):
            token=p.recording_token(self.peer.credential,self.peer.relationship,self.f.master.store.node['node_id'],
                user,recording,operation,int(time.time()),content_type='audio/webm',filename='original.webm')
            response=await self.client.get('/internal/v1/recordings/'+recording,params={'token':token})
            self.assertEqual(response.status_code,200,response.text)
            self.assertIn(self.peer.relationship+'/'+user+'/'+recording+'.bin',response.headers['X-Accel-Redirect'])
        rows=await self.rows();self.assertEqual(len(rows),2)
        self.assertTrue(all(row['object_id']==recording and row['logical_id']==recording for row in rows))
        self.assertEqual(path.read_bytes(),b'opaque recording and footer')

    async def test_expired_reap_is_bounded_and_keeps_live_migration_and_other_member_leases(self):
        node=self.peer.store.node['node_id'];now=int(time.time())
        values=[dict(lease_id=uuid.uuid4().hex,member_id=node,logical_id=self.logical,object_id=self.object_id,
            source_version='f'*64,expires_at=now-10,task_id='',purpose='playback') for _ in range(50)]
        values.extend(dict(lease_id=uuid.uuid4().hex,member_id=node,logical_id=self.logical,object_id=self.object_id,
            source_version='f'*64,expires_at=now+300,task_id=uuid.uuid4().hex if purpose=='migration' else '',purpose=purpose)
            for purpose in ('playback','migration'))
        other={**values[0],'lease_id':uuid.uuid4().hex,'member_id':self.f.target.store.node['node_id']};values.append(other)
        async with self.peer.database.begin() as conn:await conn.execute(insert(s.read_leases),values)
        await self.begin();rows=await self.rows()
        self.assertEqual(sum(row['member_id']==node and row['expires_at']<=now for row in rows),18)
        self.assertIn(other,rows);self.assertEqual(len(rows),22)
        self.assertEqual(sum(row['purpose']=='migration' for row in rows),1)

    async def test_cleanup_intent_blocks_new_read_but_keeps_the_original_and_existing_lease(self):
        lease=await self.begin();await self.cleanup_intent()
        with self.assertRaises(HTTPException) as error:await self.begin()
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual([row['lease_id'] for row in await self.rows()],[lease])
        self.assertEqual(self.path.read_bytes(),self.data)

    async def test_released_historical_cleanup_does_not_block_a_later_round_trip(self):
        await self.cleanup_intent()
        async with self.peer.database.begin() as conn:
            await conn.execute(update(s.peer_reservations).values(state='released'))
            await conn.execute(update(s.units).values(state='cleaned'))
        await self.begin();self.assertEqual(len(await self.rows()),1)

    async def test_symlink_hardlink_and_missing_resources_never_create_a_lease(self):
        moved=self.path.with_suffix('.keep');self.path.rename(moved)
        for kind in ('symlink','hardlink','missing'):
            if kind=='symlink':self.path.symlink_to(moved)
            elif kind=='hardlink':os.link(moved,self.path)
            with self.assertRaises(HTTPException) as error:await self.begin()
            self.assertEqual(error.exception.status_code,404)
            self.assertEqual(await self.rows(),[]);self.assertEqual(moved.read_bytes(),self.data)
            if kind!='missing':self.path.unlink()

    async def test_relationship_and_registry_changes_after_authorization_are_revalidated(self):
        async with self.peer.database.begin() as conn:
            await conn.execute(update(fs.relationships).values(peer_endpoint='https://changed.example.com'))
        with self.assertRaises(HTTPException) as error:await self.begin()
        self.assertEqual(error.exception.status_code,401)
        async with self.peer.database.begin() as conn:
            await conn.execute(update(fs.relationships).values(peer_endpoint=self.relation['peer_endpoint']))
            await conn.execute(text('UPDATE media_objects SET media_path=:path'),dict(path='music/renamed/song.mp3'))
        with self.assertRaises(HTTPException) as error:await self.begin()
        self.assertEqual(error.exception.status_code,409);self.assertEqual(await self.rows(),[])
        self.assertEqual(self.path.read_bytes(),self.data)

    async def test_capability_expiring_while_file_is_opened_rolls_back_the_lease(self):
        now=int(time.time());self.expires=now+5
        with patch.object(readers,'time',SimpleNamespace(time=unittest.mock.Mock(side_effect=[now,now+6]))):
            with self.assertRaises(HTTPException) as error:await self.begin()
        self.assertEqual(error.exception.status_code,401);self.assertEqual(await self.rows(),[])

    async def test_cleanup_fence_waits_until_read_lease_is_committed(self):
        entered,release,cleaning=asyncio.Event(),asyncio.Event(),asyncio.Event()
        original=self.peer.store.lock
        async def lock(conn):
            value=await original(conn);entered.set();await release.wait();return value
        async def cleanup():
            async with media_mutation_lock:
                cleaning.set()
                self.assertEqual(len(await self.rows()),1)
        with patch.object(self.peer.store,'lock',side_effect=lock):
            read=asyncio.create_task(self.begin());await asyncio.wait_for(entered.wait(),2)
            clean=asyncio.create_task(cleanup())
            try:
                await asyncio.sleep(.02);self.assertFalse(cleaning.is_set())
            finally:
                release.set();await asyncio.wait_for(read,2);await asyncio.wait_for(clean,2)
        self.assertTrue(cleaning.is_set())
