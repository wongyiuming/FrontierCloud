"""Real SQL S3 catalog/read/delete integration and irreversible-error fences."""
import importlib
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from starlette.requests import Request
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select, text, update

from app.services import media_catalog_cache as cache, media_crypto, media_directories, media_manager, resource_pool, s3_schema as s
from app.services.federation import routing, schema as fs
from app.services.federation.catalog import Catalog
from app.services.s3_control import ControlError
from app.services.s3_members import Conflict
from app.services.s3_signing import S3Error
from app.services.s3_objects import Objects
from app.services.s3_recovery import Recovery
from tests import test_s3_uploads as fixtures
from tests.test_media_catalog_cache import _FakeRedis


class S3ObjectsTests(unittest.IsolatedAsyncioTestCase):
    fixture_type = fixtures.S3UploadTests

    async def asyncSetUp(self):
        self.f = self.fixture_type(methodName='runTest')
        await self.f.asyncSetUp()
        self.f.store.node['role'] = 'Master'
        self.redis = _FakeRedis()
        self.catalog = Catalog(self.f.store)
        state_module = importlib.import_module('app.services.federation.state')
        self.patches = [patch.object(cache,'redis_client',self.redis),patch.object(state_module,'state',self.f.store),
            patch.object(routing,'state',self.f.store),patch.object(routing,'catalog',self.catalog),
            patch.object(media_directories,'node_state',self.f.store),patch.object(media_directories,'node_catalog',self.catalog)]
        for item in self.patches:
            item.start()
        self.objects = Objects(self.f.store,clock=lambda:self.f.now,control_factory=self.f.provider.control,lease_root=self.f.root/'leases')

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.f.asyncTearDown()

    async def published(self,path='music/album/song.mp3',meta=None):
        data = b'original bytes' if meta is None else b'x'*meta.ciphertext_size
        upload_id = await self.f.prepared(data,meta,path)
        result = await self.f.service.complete(upload_id,actor='admin')
        return result['media_id']

    async def test_catalog_exposes_external_adapter_without_follower_or_invented_health(self):
        media_id = await self.published()
        row = await self.catalog.resource(media_id)
        self.assertEqual((row['transport'],row['health'],row['relationship_id']),('S3','external',None))
        self.assertEqual(len(await self.catalog.resources(directory='music/album')),1)
        await self.f.members.configure(self.f.member,3,name='one',quota=100*1024**2,enabled=False,actor='admin')
        self.assertEqual((await self.catalog.resource(media_id))['resource_id'],media_id)
        await self.objects.grant(media_id)

    async def test_admin_tree_and_search_report_real_encryption_without_key_material(self):
        from app.api.v1 import admin_cluster_integrity as admin
        descriptor=media_crypto.Metadata.new(31)
        encrypted=await self.published('music/album/encrypted.mp3',descriptor)
        plain=await self.published('music/album/plain.mp3')
        with patch.object(admin,'node_state',self.f.store),patch.object(admin,'node_catalog',self.catalog):
            tree=await admin._global_tree('music/album')
            search=await admin._global_tree('music','encrypted')
        self.assertEqual({item['media_id']:item['encrypted'] for item in tree['items']},{encrypted:True,plain:False})
        self.assertEqual([(item['media_id'],item['encrypted']) for item in search['items']],[(encrypted,True)])
        for item in tree['items']+search['items']:
            self.assertEqual(item['transport'],'S3')
            self.assertNotIn('encryption',item)
            self.assertNotIn('key',item)

    async def test_admin_tree_refuses_missing_s3_descriptor_instead_of_claiming_plaintext(self):
        from app.api.v1 import admin_cluster_integrity as admin
        encrypted=await self.published(meta=media_crypto.Metadata.new(31))
        async with self.f.database.begin() as conn:
            await media_crypto.forget_encryption(conn,encrypted)
        with patch.object(admin,'node_state',self.f.store),patch.object(admin,'node_catalog',self.catalog):
            for query in (None,'song'):
                with self.assertRaisesRegex(media_crypto.MetadataError,'descriptor missing'):
                    await admin._global_tree('music/album',query)

    async def test_read_head_download_names_and_delete_deadline_are_persisted(self):
        name='中文 original (live).mp3'
        media_id = await self.published('music/album/'+name)
        get = await self.objects.grant(media_id,download=True)
        head = await self.objects.grant(media_id,method='HEAD')
        params = parse_qs(urlsplit(get.url).query)
        self.assertEqual(params['response-content-type'],['audio/mpeg'])
        self.assertIn("filename*=UTF-8''%E4%B8%AD%E6%96%87%20original%20%28live%29.mp3",params['response-content-disposition'][0])
        self.assertNotEqual(params['X-Amz-Signature'],parse_qs(urlsplit(head.url).query)['X-Amz-Signature'])
        signer,_ = await self.f.members.signer(self.f.member)
        self.assertNotIn(signer.credentials.secret_key,get.url)
        self.assertEqual((await self.objects.delete(media_id))['state'],'deleting')
        with self.assertRaisesRegex(S3Error,'object_missing'):
            await self.objects.grant(media_id)
        self.assertEqual(await self.f.capacity(),(14,0))
        self.f.now += 301
        self.assertEqual((await self.objects.delete(media_id))['state'],'deleted')
        self.assertEqual(await self.f.capacity(),(0,0))

    async def test_missing_encryption_descriptor_never_falls_back_to_plaintext(self):
        meta = media_crypto.Metadata.new(20)
        media_id = await self.published(meta=meta)
        grant = await self.objects.grant(media_id)
        self.assertEqual(parse_qs(urlsplit(grant.url).query)['response-content-type'],['application/octet-stream'])
        async with self.f.database.begin() as conn:
            await media_crypto.forget_encryption(conn,media_id)
        with self.assertRaisesRegex(Conflict,'encryption_descriptor_missing'):
            await self.objects.grant(media_id)
        with self.assertRaisesRegex(media_crypto.MetadataError,'descriptor missing'):
            await media_crypto.browser_download_plan(['music/album/song.mp3'])

    async def test_lost_delete_ack_recovers_only_owned_object_and_settles_once(self):
        media_id = await self.published()
        other = await self.published('music/album/other.mp3')
        self.f.provider.delete_lost = True
        with self.assertRaisesRegex(ControlError,'timeout'):
            await self.objects.delete(media_id)
        self.assertEqual(await self.f.capacity(),(28,0))
        result = await Recovery(self.f.store,service=self.f.service,clock=lambda:self.f.now).once()
        self.assertEqual(result['closed'],1)
        self.assertEqual(await self.f.capacity(),(14,0))
        self.assertEqual((await self.objects.delete(media_id))['state'],'deleted')
        self.assertEqual((await self.catalog.resource(other))['resource_id'],other)

    async def test_mismatched_object_identity_is_not_deleted_or_charged_as_removed(self):
        media_id = await self.published()
        self.f.provider.head_change = dict(object_id='f'*64)
        with self.assertRaisesRegex(Conflict,'delete_identity_mismatch'):
            await self.objects.delete(media_id)
        self.assertEqual(await self.f.capacity(),(14,0))
        self.assertTrue(self.f.provider.objects)
        with self.assertRaises(HTTPException):
            await routing.resolve(media_id)

    async def test_lost_sql_commit_after_delete_preserves_business_rows_until_retry(self):
        media_id = await self.published()
        async with self.f.database.begin() as conn:
            await conn.execute(text("INSERT INTO media_playback_stats(media_id,media_path,play_score,preference,created_at,updated_at) VALUES(:id,'music/album/song.mp3',17,19,'2026-01-01','2026-01-01')"),{'id':media_id})
        original = self.f.store.log
        async def fail(conn,action,*args,**kwargs):
            if action == 's3_media_deleted':
                raise RuntimeError('audit failure')
            return await original(conn,action,*args,**kwargs)
        with patch.object(self.f.store,'log',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'audit failure'):
                await self.objects.delete(media_id)
        async with self.f.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT play_score FROM media_playback_stats WHERE media_id=:id'),{'id':media_id}),17)
        self.assertEqual(await self.f.capacity(),(14,0))
        await self.objects.delete(media_id)
        self.assertEqual(await self.f.capacity(),(0,0))

    async def test_sql_namespace_prevents_stale_catalog_after_lost_redis_invalidation(self):
        await self.published()
        generation,_ = await cache.load_media_catalog('tracks','s3-test')
        await cache.store_media_catalog(generation,'tracks','s3-test',[dict(old=True)])
        with patch.object(self.redis,'incr',side_effect=RedisConnectionError('offline')):
            await self.published('music/album/other.mp3')
        changed,cached = await cache.load_media_catalog('tracks','s3-test')
        self.assertNotEqual(generation,changed)
        self.assertIsNone(cached)

    async def test_directory_rename_changes_only_logical_paths_and_retains_identity(self):
        media_id = await self.published()
        async with self.f.database.connect() as conn:
            key = await conn.scalar(select(s.objects.c.object_key))
        with patch.object(media_directories,'_remote_rename',new=AsyncMock(side_effect=AssertionError('S3 cannot copy through Follower'))),\
             patch.object(media_directories,'_rename_physical_directory',side_effect=AssertionError('S3 is not local')):
            async with media_manager.media_mutation_lock:
                await media_directories.rename_directory('music/album','renamed')
        self.assertEqual((await self.catalog.resource(media_id))['path'],'music/renamed/song.mp3')
        async with self.f.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.objects.c.object_key)),key)
        self.assertEqual(len(self.f.provider.objects),1)

    async def test_plain_s3_download_uses_browser_and_never_a_master_zip_source(self):
        media_id = await self.published()
        with patch.object(media_crypto,'safe_open_media',side_effect=AssertionError('S3 must not open local bytes')):
            plan = await media_crypto.browser_download_plan(['music/album'])
        self.assertEqual(plan[0]['media_id'],media_id)
        self.assertTrue(plan[0]['browser_download'])
        self.assertNotIn('encryption',plan[0])

    async def test_stream_resolves_s3_before_local_fallback_and_uses_head_signature(self):
        media_id = await self.published()
        with patch('app.services.s3_objects.Objects',return_value=self.objects):
            get = await routing.stream(media_id,'music/album/song.mp3')
            head = await routing.stream(media_id,'music/album/song.mp3',method='HEAD')
        self.assertEqual(get.status_code,307)
        self.assertNotIn('x-accel-redirect',get.headers)
        self.assertNotEqual(get.headers['location'],head.headers['location'])
        self.assertEqual(get.headers['cache-control'],'no-store')

    async def test_snapshot_download_sets_attachment_at_s3_and_public_playback_stays_inline(self):
        from app.api.v1 import media_crypto as http
        media_id=await self.published()
        async with self.f.database.connect() as conn:
            source=dict((await conn.execute(select(fs.global_media).where(fs.global_media.c.media_id==media_id))).mappings().one())
        snapshot=media_crypto.download_snapshot(media_id,source['media_path'],source['size_bytes'],media_crypto._global_revision(source))
        request=Request(dict(type='http',method='GET',path='/',headers=[]))
        authorized=dict(media_id=media_id,resource_id=media_id,encryption=None,size_bytes=source['size_bytes'])
        with patch.object(http.crypto,'_crypto_object',new=AsyncMock(return_value=authorized)),\
             patch('app.services.s3_objects.Objects',return_value=self.objects),\
             patch.object(http.crypto,'safe_open_media',side_effect=AssertionError('S3 download cannot open Master media')):
            download=await http.raw_bytes(request,source['media_path'],admin=True,expected_id=media_id,snapshot=snapshot)
            playback=await routing.stream(media_id,source['media_path'])
        download_query=parse_qs(urlsplit(download.headers['location']).query)
        playback_query=parse_qs(urlsplit(playback.headers['location']).query)
        self.assertTrue(download_query['response-content-disposition'][0].startswith('attachment;'))
        self.assertNotIn('response-content-disposition',playback_query)

    async def test_generic_delete_intent_and_s3_recovery_marker_commit_together(self):
        media_id=await self.published()
        generation,_=await cache.load_media_catalog('tracks','atomic-delete')
        await cache.store_media_catalog(generation,'tracks','atomic-delete',[dict(old=True)])
        with patch.object(self.redis,'incr',side_effect=RedisConnectionError('offline')):
            await resource_pool.mark_pending_delete([media_id],self.f.database)
        changed,cached=await cache.load_media_catalog('tracks','atomic-delete')
        self.assertNotEqual(generation,changed)
        self.assertIsNone(cached)
        async with self.f.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.objects.c.state)),'deleting')
        self.assertEqual((await Recovery(self.f.store,service=self.f.service,clock=lambda:self.f.now).once())['closed'],1)
        self.assertEqual(await self.f.capacity(),(0,0))

    async def test_delete_intent_audit_failure_rolls_back_catalog_binding_and_cache_revision(self):
        media_id=await self.published()
        generation,_=await cache.load_media_catalog('tracks','atomic-delete-failure')
        with patch.object(self.f.store,'log',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaisesRegex(RuntimeError,'audit unavailable'):
                await resource_pool.mark_pending_delete([media_id],self.f.database)
        self.assertEqual((await cache.load_media_catalog('tracks','atomic-delete-failure'))[0],generation)
        self.assertEqual((await self.catalog.resource(media_id))['resource_id'],media_id)
        async with self.f.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.objects.c.state)),'active')
