"""Actual Python artwork responses and lazy category/view boundaries."""
from pathlib import Path
from collections import OrderedDict
import hashlib
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx
from sqlalchemy import insert, text, update
from sqlalchemy.exc import SQLAlchemyError

from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.api.v1 import media, music_artwork as a
from app.services import media_crypto, media_manager, media_objects, music_tags as m
from app.services.federation import schema as fs
from app.services.federation.state import state
from tests.test_music_tags import JPEG, fixture


class MusicArtworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = FastAPI()
        app.include_router(a.router, prefix='/api/v1/media')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://master.4399.com')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_cover_keeps_original_bytes_conditional_cache_and_head(self):
        with patch.object(m, 'music_metadata', AsyncMock(return_value=m.Tags(picture=JPEG, mime='image/jpeg'))) as tags:
            url = '/api/v1/media/cover?file_path=music%2Fartist%2Fsong.mp3'
            response = await self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, JPEG)
            self.assertEqual(response.headers['content-type'], 'image/jpeg')
            self.assertIn('private', response.headers['cache-control'])
            head = await self.client.head(url)
            self.assertEqual(head.content, b'')
            self.assertEqual(int(head.headers['content-length']), len(JPEG))
            cached = await self.client.get(url, headers={'If-None-Match': 'W/' + response.headers['etag']})
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.content, b'')
            self.assertEqual(tags.await_count, 3, 'conditional image reads still need current media authorization')

    async def test_absent_and_encrypted_artwork_are_not_server_fallbacks(self):
        with patch.object(m, 'music_metadata', AsyncMock(return_value=m.Tags())):
            response = await self.client.get('/api/v1/media/cover?file_path=music/artist/encrypted.mp3')
        self.assertEqual(response.status_code, 404)
        with patch.object(m, 'music_metadata', AsyncMock(side_effect=ValueError('invalid path'))):
            response = await self.client.get('/api/v1/media/cover?file_path=../../private.pem')
        self.assertEqual(response.status_code, 404)

    async def test_plain_tracks_are_decorated_and_encrypted_metadata_is_not_inspected(self):
        tracks = [
            {'media_path': 'music/a/one.mp3', 'type': 'audio', 'title': 'one', 'cover': '/favicon.ico'},
            {'media_path': 'music/a/two.mp3', 'type': 'audio', 'title': 'two', 'cover': '/favicon.ico'},
            {'media_path': 'music/a/secret.mp3', 'type': 'audio', 'encrypted': True, 'encryption': {'file_id': 'secret'}, 'cover': '/favicon.ico'},
        ]
        with patch.object(m, 'music_metadata', AsyncMock(side_effect=[m.Tags(title='Title', album='Album', picture=JPEG), m.Tags(album='Album')])) as metadata:
            result = await m.decorate_tracks(tracks)
        self.assertEqual(metadata.await_count, 2)
        self.assertEqual(result[0]['title'], 'Title')
        self.assertTrue(result[0]['cover'].startswith('/api/v1/media/cover?'))
        self.assertEqual(result[1]['cover'], result[0]['cover'])
        self.assertEqual(result[2]['cover'], '/favicon.ico')

    async def test_category_listing_adds_only_lazy_urls_without_reading_media(self):
        entries = [{'name': 'Artist', 'url': '/api/v1/media/music/category?path=music%2FArtist'}]
        with patch.object(m, 'music_metadata', AsyncMock(side_effect=AssertionError('eager image read'))):
            result = a.category_artwork(entries, 'music', False)
        self.assertNotIn('artwork_url', entries[0])
        self.assertEqual(result[0]['artwork_url'], '/api/v1/media/catalog/artwork?path=music%2FArtist')
        self.assertEqual(a.category_artwork(entries, 'video', False), entries)

    async def test_optional_metadata_failure_does_not_fail_the_playable_catalog(self):
        tracks = [{'media_path': 'music/a/one.mp3', 'type': 'audio', 'title': 'Original name', 'cover': '/favicon.ico'}]
        with patch.object(m, 'music_metadata', AsyncMock(side_effect=SQLAlchemyError('metadata query unavailable'))):
            result = await m.decorate_tracks(tracks)
        self.assertEqual(result[0]['title'], 'Original name')
        self.assertEqual(result[0]['cover'], '/favicon.ico')

    async def test_directory_prefers_original_cover_or_at_most_eight_cipher_candidates(self):
        tracks = [{'media_path': f'music/a/{i}.mp3', 'type': 'audio', 'encryption': {'file_id': str(i)}} for i in range(20)]
        with patch.object(media, '_public_category_parts', AsyncMock(return_value=('music', 'a'))), \
                patch.object(media, 'scan_media_files_by_category', AsyncMock(return_value=tracks)), \
                patch.object(m, 'decorate_tracks', AsyncMock(side_effect=lambda items: items)):
            response = await self.client.get('/api/v1/media/catalog/artwork?path=music/a')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['metadata_tracks']), 8)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        tracks.append({'media_path': 'music/a/plain.mp3', 'cover': '/api/v1/media/cover?file_path=music/a/plain.mp3'})
        with patch.object(media, '_public_category_parts', AsyncMock(return_value=('music', 'a'))), \
                patch.object(media, 'scan_media_files_by_category', AsyncMock(return_value=tracks)), \
                patch.object(m, 'decorate_tracks', AsyncMock(side_effect=lambda items: items)):
            response = await self.client.get('/api/v1/media/catalog/artwork?path=music/a')
        self.assertTrue(response.json()['cover'])
        self.assertEqual(response.json()['metadata_tracks'], [])

    async def test_artist_with_only_albums_keeps_all_candidates_bounded(self):
        async def tracks(path, *_args, **_kwargs):
            if path == 'music/a':
                return []
            return [{'media_path': f'{path}/{i}.mp3', 'encryption': {'file_id': str(i)}} for i in range(10)]
        with patch.object(media, 'scan_media_files_by_category', AsyncMock(side_effect=tracks)), \
                patch.object(media, 'get_media_subcategories', AsyncMock(return_value=[{'name': 'first'}, {'name': 'second'}])), \
                patch.object(m, 'decorate_tracks', AsyncMock(side_effect=lambda items: items)):
            result = await a._artwork('music/a', False)
        self.assertEqual(len(result['metadata_tracks']), 8)

    async def test_actual_category_template_resolves_new_assets_and_business_back_link(self):
        with patch.object(media, 'node_stun_urls', AsyncMock(return_value=[])):
            response = await media._render_category_page('前沿音乐 - ' + 'long' * 100,
                                                         '/api/v1/media/music', {'bootstrap': [], 'url': ''})
        html = response.body.decode()
        self.assertNotIn('{{', html)
        self.assertIn('href="/api/v1/media/music"', html)
        self.assertIn('/static/js/business-navigation.js?v=', html)
        self.assertIn('/static/js/music-metadata.js?v=', html)
        self.assertIn('/static/css/directory-globe.css?v=', html)
        self.assertLess(html.index('js/compiled/media-crypto.js'), html.index('js/music-metadata.js'))


class MusicArtworkSQLiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        (self.media / 'music/artist').mkdir(parents=True)
        self.database = create_sqlite_engine('sqlite+aiosqlite:///' + str(self.root / 'database.sqlite'))
        self.patches = [patch.object(db, 'engine', self.database), patch.object(db, 'SECRET_DIR', self.root / 'vault'),
                        patch.object(media_objects, 'engine', self.database), patch.object(media_manager, 'engine', self.database),
                        patch.object(media_manager, 'MEDIA_ROOT', self.media), patch.object(media_manager, 'ensure_media_mutations_ready'),
                        patch.object(state, 'node', {'role': 'Standalone', 'node_id': 'a' * 32})]
        for item in self.patches:
            item.start()
        await db.init_db()
        async with self.database.begin() as conn:
            await conn.execute(insert(fs.identity).values(singleton=1, node_id='a' * 32, role='Standalone', endpoint='', private_key='unused fixture key', created_at=1))
        app = FastAPI()
        app.include_router(a.router, prefix='/api/v1/media')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost')

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        await self.database.dispose()
        self.temp.cleanup()

    async def test_real_database_authorizes_cover_and_denies_hidden_cached_cover(self):
        path = self.media / 'music/artist/song.mp3'
        content = fixture() + bytes(256 * 1024)
        path.write_bytes(content)
        url = '/api/v1/media/cover?file_path=music/artist/song.mp3'
        first = await self.client.get(url)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.content, JPEG)
        async with self.database.begin() as conn:
            await conn.execute(text('INSERT INTO media_visibility(relative_path,hidden,updated_at) VALUES(:path,1,CURRENT_TIMESTAMP)'), {'path': 'music/artist'})
        denied = await self.client.get(url, headers={'If-None-Match': first.headers['etag']})
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(path.read_bytes(), content)

    async def test_real_encryption_descriptor_and_symlink_cannot_be_read_as_cover(self):
        path = 'music/artist/cipher.mp3'
        meta = media_crypto.Metadata.new(100)
        (self.media / path).write_bytes(bytes(meta.ciphertext_size))
        async with self.database.begin() as conn:
            identifier = await media_objects.ensure_object(conn, path, 'audio')
            await media_crypto.put_encryption(conn, identifier, meta)
        with patch('app.middleware.native_storage._open_beneath', side_effect=AssertionError('server opened encrypted content')):
            response = await self.client.get('/api/v1/media/cover?file_path=' + path)
        self.assertEqual(response.status_code, 404)
        outside = self.root / 'outside.mp3';outside.write_bytes(fixture())
        (self.media / 'music/artist/link.mp3').symlink_to(outside)
        response = await self.client.get('/api/v1/media/cover?file_path=music/artist/link.mp3')
        self.assertEqual(response.status_code, 404)

    async def remote_object(self):
        now=int(time.time());path='music/remote/song.mp3';identifier='d'*64
        async with self.database.begin() as conn:
            await conn.execute(update(fs.identity).values(role='Master'))
            await conn.execute(insert(fs.relationships).values(relationship_id='b'*32,peer_id='c'*32,
                peer_endpoint='https://storage.4399.com',peer_key='unused public fixture key',credential='unused sealed fixture credential',
                direction='downstream',mode='Direct',state='active',status='online',last_heartbeat=now,rtt_ms=1,
                failures=0,recoveries=0,peer_version='fixture',protocol=1,summary={},created_at=now))
            await conn.execute(insert(fs.storage_members).values(member_id='c'*32,relationship_id='b'*32,
                member_kind='Follower',transport='Direct',storage_enabled=1,allocated_bytes=1024**3,
                used_bytes=len(fixture()),reserved_bytes=0,physical_free_bytes=1024**3,health='online',writable=1,updated_at=now))
            await conn.execute(insert(fs.global_media).values(media_id=identifier,storage_member_id='c'*32,object_id='e'*64,
                media_path=path,path_locator=hashlib.sha256(path.encode()).hexdigest(),object_kind='audio',
                size_bytes=len(fixture()),etag='first-source-version',state='active',created_at=now,updated_at=now))
        return path,identifier

    async def test_real_remote_catalog_shape_authorizes_original_cover_and_invalidates_changed_source(self):
        path,identifier=await self.remote_object();url='/api/v1/media/cover?file_path='+path+'&resource_id='+identifier
        with patch.object(state,'node',{'role':'Master','node_id':'a'*32}),patch.object(state,'database',self.database), \
                patch.object(m,'_cache',OrderedDict()),patch.object(m,'_cache_bytes',0), \
                patch.object(m,'_remote_tags',AsyncMock(return_value=m.Tags(picture=JPEG,mime='image/jpeg'))) as reads:
            for _ in range(2):
                response=await self.client.get(url);self.assertEqual(response.status_code,200);self.assertEqual(response.content,JPEG)
            self.assertEqual(reads.await_count,1)
            async with self.database.begin() as conn:
                await conn.execute(update(fs.global_media).where(fs.global_media.c.media_id==identifier).values(etag='second-source-version'))
            response=await self.client.get(url);self.assertEqual(response.status_code,200);self.assertEqual(reads.await_count,2)
            self.assertEqual(reads.await_args.args[0]['payload']['etag'],'second-source-version')
            self.assertNotIn('etag',reads.await_args.args[0])

    async def test_remote_source_change_during_metadata_read_is_a_conflict_not_a_cached_cover(self):
        path,identifier=await self.remote_object()
        async def replace_source(*_arguments):
            async with self.database.begin() as conn:
                await conn.execute(update(fs.global_media).where(fs.global_media.c.media_id==identifier).values(etag='changed-during-read'))
            return m.Tags(picture=JPEG,mime='image/jpeg')
        with patch.object(state,'node',{'role':'Master','node_id':'a'*32}),patch.object(state,'database',self.database), \
                patch.object(m,'_cache',OrderedDict()),patch.object(m,'_cache_bytes',0), \
                patch.object(m,'_remote_tags',side_effect=replace_source):
            response=await self.client.get('/api/v1/media/cover?file_path='+path+'&resource_id='+identifier)
            self.assertEqual(response.status_code,409)
            self.assertFalse(m._cache)
