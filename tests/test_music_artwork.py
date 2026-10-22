"""Actual Python artwork responses and lazy category/view boundaries."""
from pathlib import Path
from collections import OrderedDict
import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx
from sqlalchemy import insert, text, update

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

    async def test_plain_tracks_offer_lazy_metadata_without_inspecting_any_source(self):
        tracks = [
            {'media_path': 'music/a/one.mp3', 'type': 'audio', 'title': 'one', 'cover': '/favicon.ico'},
            {'media_path': 'music/a/s3.mp3', 'type': 'audio', 'browser_metadata': True},
            {'media_path': 'music/a/secret.mp3', 'type': 'audio', 'encrypted': True, 'encryption': {'file_id': 'secret'}, 'cover': '/favicon.ico'},
            {'media_path': 'music/a/other.wav', 'type': 'audio'},
        ]
        with patch.object(m, 'music_metadata', AsyncMock(side_effect=AssertionError('catalog read media'))) as metadata:
            result = m.lazy_tracks(tracks)
        metadata.assert_not_awaited()
        self.assertTrue(result[0]['native_metadata'])
        self.assertEqual(result[0]['title'], 'one')
        self.assertEqual(result[0]['cover'], '/favicon.ico')
        self.assertTrue(all('native_metadata' not in entry for entry in result[1:]))
        self.assertEqual(result[2]['cover'], '/favicon.ico')

    async def test_category_listing_adds_only_lazy_urls_without_reading_media(self):
        entries = [{'name': 'Artist', 'url': '/api/v1/media/music/category?path=music%2FArtist'}]
        with patch.object(m, 'music_metadata', AsyncMock(side_effect=AssertionError('eager image read'))):
            result = a.category_artwork(entries, 'music', False)
        self.assertNotIn('artwork_url', entries[0])
        self.assertEqual(result[0]['artwork_url'], '/api/v1/media/catalog/artwork?path=music%2FArtist')
        self.assertEqual(a.category_artwork(entries, 'video', False), entries)

    async def test_pending_optional_metadata_cannot_block_the_complete_playable_catalog(self):
        tracks = [{'media_path': f'music/a/{i}.mp3', 'media_id': str(i), 'type': 'audio',
                   'title': str(i), 'cover': '/favicon.ico'} for i in range(1000)]
        async def stalled(*_args):
            await asyncio.Event().wait()
        with patch.object(media, '_public_category_parts', AsyncMock()), \
                patch.object(media, 'scan_media_files_by_category', AsyncMock(return_value=tracks)), \
                patch.object(media.playback, 'attach_stats_and_sort', AsyncMock(return_value=tracks)), \
                patch.object(media.lyrics, 'attach_links', AsyncMock(return_value=tracks)), \
                patch.object(media.karaoke_identity, 'attach', return_value=None), \
                patch.object(m, 'music_metadata', AsyncMock(side_effect=stalled)) as metadata:
            response = await asyncio.wait_for(media.get_media_catalog_data('music', 'music/a', 'test-session', False), 0.5)
        entries = json.loads(response.body)['entries']
        self.assertEqual(len(entries), 1000)
        self.assertEqual([entry['media_id'] for entry in entries], [str(i) for i in range(1000)])
        self.assertTrue(all(entry['native_metadata'] and entry['cover'] == '/favicon.ico' for entry in entries))
        metadata.assert_not_awaited()

    async def test_directory_returns_eight_lazy_candidates_without_reading_any_tags(self):
        tracks = [{'media_path': f'music/a/{i}.mp3', 'type': 'audio', 'title': str(i)} for i in range(1000)]
        tracks[0]['encryption'] = {'file_id': 'cipher'}
        tracks[1]['browser_metadata'] = True
        with patch.object(media, '_public_category_parts', AsyncMock(return_value=('music', 'a'))), \
                patch.object(media, 'scan_media_files_by_category', AsyncMock(return_value=tracks)) as scans, \
                patch.object(m, 'music_metadata', AsyncMock(side_effect=AssertionError('artwork read media'))) as reads:
            response = await self.client.get('/api/v1/media/catalog/artwork?path=music/a')
        self.assertEqual(response.status_code, 200)
        candidates = response.json()['metadata_tracks']
        self.assertEqual(len(candidates), 8)
        self.assertEqual([entry['title'] for entry in candidates], [str(i) for i in range(8)])
        self.assertNotIn('native_metadata', candidates[0])
        self.assertNotIn('native_metadata', candidates[1])
        self.assertTrue(all(entry['native_metadata'] for entry in candidates[2:]))
        self.assertEqual(response.json()['cover'], '')
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        scans.assert_awaited_once()
        reads.assert_not_awaited()
        self.assertNotIn('native_metadata', tracks[2], 'optional artwork must not mutate the cached full catalog')

    async def test_artist_with_only_albums_keeps_all_candidates_bounded(self):
        async def tracks(path, *_args, **_kwargs):
            if path == 'music/a':
                return []
            return [{'media_path': f'{path}/{i}.mp3', 'encryption': {'file_id': str(i)}} for i in range(10)]
        with patch.object(media, 'scan_media_files_by_category', AsyncMock(side_effect=tracks)) as scans, \
                patch.object(media, 'get_media_subcategories', AsyncMock(return_value=[{'name': str(i)} for i in range(100)])), \
                patch.object(m, 'music_metadata', AsyncMock(side_effect=AssertionError('artwork read media'))) as reads:
            result = await a._artwork('music/a', False)
        self.assertEqual(len(result['metadata_tracks']), 8)
        self.assertEqual(scans.await_count, 2, 'a full candidate budget must not traverse the other 99 albums')
        reads.assert_not_awaited()

    async def test_artist_directory_and_track_inspection_budgets_are_shared(self):
        for empty in (True, False):
            async def tracks(path, *_args, **_kwargs):
                return [] if empty or path == 'music/a' else [{'media_path': path + '/song.wav', 'type': 'audio'}]
            with self.subTest(empty=empty), \
                    patch.object(media, 'scan_media_files_by_category', AsyncMock(side_effect=tracks)) as scans, \
                    patch.object(media, 'get_media_subcategories', AsyncMock(return_value=[{'name': str(i)} for i in range(100)])):
                result = await a._artwork('music/a', False)
            self.assertEqual(scans.await_count, 8, 'the root and children share a finite visit budget')
            self.assertEqual(result, {'cover': '', 'metadata_tracks': []})

    async def test_artwork_deadline_cancels_pending_scan_and_keeps_optional_result(self):
        cancelled = asyncio.Event()
        async def stalled(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(a, 'ARTWORK_TIMEOUT_SECONDS', 0.01), \
                patch.object(media, 'scan_media_files_by_category', side_effect=stalled):
            result = await asyncio.wait_for(a._artwork('music/a', False), 0.5)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result, {'cover': '', 'metadata_tracks': []})

    async def test_single_track_metadata_is_small_and_keeps_original_picture_separate(self):
        with patch.object(m, 'music_metadata', AsyncMock(return_value=m.Tags(
                title='Title', artist='Artist', album='Album', picture=JPEG, mime='image/jpeg'))) as reads:
            response = await self.client.get('/api/v1/media/metadata?file_path=music/a/one.mp3&resource_id=' + 'a' * 64)
        self.assertEqual(response.status_code, 200)
        value = response.json()
        self.assertEqual({key: value[key] for key in ('title', 'artist', 'album')},
                         {'title': 'Title', 'artist': 'Artist', 'album': 'Album'})
        self.assertEqual(value['cover'], '/api/v1/media/cover?file_path=music%2Fa%2Fone.mp3&resource_id=' + 'a' * 64)
        self.assertNotIn('picture', value)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        reads.assert_awaited_once_with('music/a/one.mp3', 'a' * 64)

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

    async def test_real_single_track_json_reauthorizes_cached_tags_without_embedding_the_picture(self):
        path = self.media / 'music/artist/song.mp3'
        original = fixture() + bytes(256 * 1024)
        path.write_bytes(original)
        url = '/api/v1/media/metadata?file_path=music/artist/song.mp3'
        with patch.object(m, '_cache', OrderedDict()), patch.object(m, '_cache_bytes', 0), \
                patch.object(m, '_joined_read', wraps=m._joined_read) as reads:
            for _ in range(2):
                response = await self.client.get(url)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['title'], '你好')
                self.assertEqual(response.json()['artist'], '達明一派')
                self.assertNotIn('picture', response.json())
                self.assertTrue(response.json()['cover'].startswith('/api/v1/media/cover?'))
            self.assertEqual(reads.await_count, 1)
            async with self.database.begin() as conn:
                await conn.execute(text('INSERT INTO media_visibility(relative_path,hidden,updated_at) VALUES(:path,1,CURRENT_TIMESTAMP)'), {'path': 'music/artist'})
            denied = await self.client.get(url)
            self.assertEqual(denied.status_code, 404)
            self.assertEqual(reads.await_count, 1, 'a cached optional tag cannot bypass current visibility')
        self.assertEqual(path.read_bytes(), original)

    async def test_real_encryption_descriptor_and_symlink_cannot_be_read_as_cover(self):
        path = 'music/artist/cipher.mp3'
        meta = media_crypto.Metadata.new(100)
        (self.media / path).write_bytes(bytes(meta.ciphertext_size))
        async with self.database.begin() as conn:
            identifier = await media_objects.ensure_object(conn, path, 'audio')
            await media_crypto.put_encryption(conn, identifier, meta)
        with patch('app.middleware.native_storage._open_beneath', side_effect=AssertionError('server opened encrypted content')):
            response = await self.client.get('/api/v1/media/cover?file_path=' + path)
            metadata = await self.client.get('/api/v1/media/metadata?file_path=' + path)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.json(), {})
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

    async def test_json_source_change_during_optional_read_is_also_a_conflict(self):
        path, identifier = await self.remote_object()
        async def replace_source(*_arguments):
            async with self.database.begin() as conn:
                await conn.execute(update(fs.global_media).where(fs.global_media.c.media_id == identifier).values(etag='changed-during-json-read'))
            return m.Tags(title='Obsolete title', picture=JPEG, mime='image/jpeg')
        with patch.object(state, 'node', {'role': 'Master', 'node_id': 'a' * 32}), patch.object(state, 'database', self.database), \
                patch.object(m, '_cache', OrderedDict()), patch.object(m, '_cache_bytes', 0), \
                patch.object(m, '_remote_tags', side_effect=replace_source):
            response = await self.client.get('/api/v1/media/metadata?file_path=' + path + '&resource_id=' + identifier)
            self.assertFalse(m._cache)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('Obsolete title', response.text)
