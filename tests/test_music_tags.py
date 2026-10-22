"""Original metadata, bounded reads, current access and joined cancellation."""
import asyncio
from collections import OrderedDict
import io
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import httpx

from app.services import media_manager, music_tags as m
from app.services.federation import protocol as p
from app.services.federation.state import state


def safe(number):
    return bytes((number >> shift) & 127 for shift in (21, 14, 7, 0))


def frame(version, name, body, flags=0):
    if version == 2:
        return name.encode() + len(body).to_bytes(3, 'big') + body
    length = safe(len(body)) if version == 4 else len(body).to_bytes(4, 'big')
    return name.encode() + length + bytes((0, flags)) + body


def tagged(version, *frames, flags=0):
    body = b''.join(frames)
    return b'ID3' + bytes((version, 0, flags)) + safe(len(body)) + body


JPEG = b'\xff\xd8\xff\xe0\0\x10JFIF\0' + bytes(600)


def fixture(version=4):
    names = ('TT2', 'TP1', 'TAL', 'PIC') if version == 2 else ('TIT2', 'TPE1', 'TALB', 'APIC')
    picture = b'\x03JPG\x03\0' if version == 2 else b'\x03image/jpeg\0\x03\0'
    return tagged(version, frame(version, names[0], b'\x01\xff\xfe' + '你好'.encode('utf-16-le')),
                  frame(version, names[1], b'\x03' + '達明一派'.encode()),
                  frame(version, names[2], b'\0More'), frame(version, names[3], picture + JPEG))


class MusicTagParsingTests(unittest.TestCase):
    def test_three_versions_preserve_text_and_original_picture(self):
        for version in (2, 3, 4):
            with self.subTest(version=version):
                tags = m.read_tags(io.BytesIO(fixture(version)))
                self.assertEqual((tags.title, tags.artist, tags.album), ('你好', '達明一派', 'More'))
                self.assertEqual((tags.picture, tags.mime), (JPEG, 'image/jpeg'))

    def test_data_length_indicator_unsynchronization_and_front_picture(self):
        body = b'\x03image/jpeg\0\x03\0' + JPEG
        encoded = body.replace(b'\xff', b'\xff\0')
        tagged_picture = frame(4, 'APIC', safe(len(body)) + encoded, 3)
        other = frame(4, 'APIC', b'\x03image/jpeg\0\x04\0' + JPEG + b'back')
        tags = m.read_tags(io.BytesIO(tagged(4, other, tagged_picture)))
        self.assertEqual(tags.picture, JPEG)
        invalid = frame(4, 'TIT2', b'\x80\0\0\0\x03invalid', 1)
        self.assertFalse(m.read_tags(io.BytesIO(tagged(4, invalid))).title)

    def test_malformed_oversized_prefix_keeps_id3v1_fallback_without_body_read(self):
        tail = b'TAG' + b'Tail title'.ljust(30, b'\0') + b'Tail artist'.ljust(30, b'\0') + b'Tail album'.ljust(30, b'\0') + bytes(35)
        data = b'ID3\x04\0\0' + safe(m.TAG_LIMIT + 1) + tail
        class Bounded(io.BytesIO):
            def read(self, count=-1):
                if count not in (10, 128):
                    raise AssertionError('unbounded or unnecessary prefix read')
                return super().read(count)
        result = m.read_tags(Bounded(data), len(data))
        self.assertEqual((result.title, result.artist, result.album), ('Tail title', 'Tail artist', 'Tail album'))

    def test_active_and_oversize_images_compressed_frames_and_many_frames_are_optional(self):
        for image in (b'<svg onload=alert(1)></svg>', JPEG + bytes(m.PICTURE_LIMIT)):
            result = m.read_tags(io.BytesIO(tagged(4, frame(4, 'APIC', b'\x03image/jpeg\0\x03\0' + image))))
            self.assertFalse(result.picture)
        compressed = tagged(3, frame(3, 'TIT2', b'\x03compressed', 0x80))
        self.assertFalse(m.read_tags(io.BytesIO(compressed)).title)
        body = [frame(4, 'TXXX', b'\0')] * m.FRAME_LIMIT + [frame(4, 'TIT2', b'\x03later')]
        self.assertFalse(m.read_tags(io.BytesIO(tagged(4, *body))).title)

    def test_cache_evicts_by_bytes_and_count(self):
        with patch.object(m, '_cache', OrderedDict()), patch.object(m, '_cache_bytes', 0), patch.object(m, 'CACHE_LIMIT', 2048):
            for index in range(20):
                m._remember(str(index), m.Tags(title='title', picture=bytes(500)))
            self.assertLessEqual(m._cache_bytes, 2048)
            self.assertIsNone(m._cached('0'))
            self.assertIsNotNone(m._cached('19'))


class MusicMetadataRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'music/artist/song.mp3'
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(fixture())
        self.obj = {'media_id': 'a' * 64, 'path': 'music/artist/song.mp3', 'kind': 'audio',
                    'size_bytes': self.path.stat().st_size, 'encryption': None, 'resource_id': ''}
        self.authorize = AsyncMock(return_value=self.obj)
        self.patches = [patch.object(media_manager, 'MEDIA_ROOT', self.root),
                        patch.object(media_manager, 'ensure_media_mutations_ready'),
                        patch('app.services.media_crypto._crypto_object', self.authorize),
                        patch.object(state, 'node', {'role': 'Standalone'}),
                        patch.object(m, '_cache', OrderedDict()), patch.object(m, '_cache_bytes', 0)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def test_cache_hits_still_reauthorize_and_file_replacement_is_not_reused(self):
        with patch.object(m, '_joined_read', wraps=m._joined_read) as reads:
            for _ in range(2):
                self.assertEqual((await m.music_metadata(self.obj['path'])).picture, JPEG)
            self.assertEqual(reads.await_count, 1)
            self.assertEqual(self.authorize.await_count, 2)
            self.authorize.side_effect = HTTPException(404, 'hidden')
            with self.assertRaises(HTTPException):
                await m.music_metadata(self.obj['path'])
            self.assertEqual(reads.await_count, 1)
            self.authorize.side_effect = None
            self.path.write_bytes(fixture()[:-1] + b'changed')
            self.obj['size_bytes'] = self.path.stat().st_size
            await m.music_metadata(self.obj['path'])
            self.assertEqual(reads.await_count, 2)

    async def test_encrypted_objects_never_open_tags_or_decrypt_on_the_server(self):
        self.obj['encryption'] = object()
        with patch('app.middleware.native_storage._open_beneath', side_effect=AssertionError('ciphertext opened')):
            self.assertEqual(await m.music_metadata(self.obj['path']), m.Tags())

    async def test_plain_s3_tags_remain_browser_only_without_a_native_or_object_read(self):
        from app.services.federation import routing
        self.obj['resource_id'] = 'd' * 64
        with patch.object(state, 'node', {'role': 'Master'}), \
                patch.object(routing, 'resolve', AsyncMock(return_value=({'transport': 'S3'}, None))), \
                patch.object(m, '_remote_tags', AsyncMock(side_effect=AssertionError('S3 bytes relayed'))) as remote, \
                patch('app.middleware.native_storage._open_beneath', side_effect=AssertionError('S3 opened locally')):
            self.assertEqual(await m.music_metadata(self.obj['path'], self.obj['resource_id']), m.Tags())
        self.authorize.assert_awaited_once_with(self.obj['path'], self.obj['resource_id'])
        remote.assert_not_awaited()

    async def test_cancelled_reads_join_before_releasing_the_mutation_lease(self):
        entered, release = threading.Event(), threading.Event()
        writer_entered = asyncio.Event()
        def blocked(_reader, _size):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test did not release the joined reader')
            return m.Tags(title='joined')
        async def writer():
            async with media_manager.media_mutation_lock:
                writer_entered.set()
        with patch.object(m, 'read_tags', blocked):
            task = asyncio.create_task(m.music_metadata(self.obj['path']))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            waiting = asyncio.create_task(writer())
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            self.assertFalse(writer_entered.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(waiting, 2)
        self.assertTrue(writer_entered.is_set())

    async def test_shutdown_cancelling_worker_still_joins_its_actual_file_read(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(_reader, _size):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test reader was not released')
            return m.Tags()
        with patch.object(m, 'read_tags', blocked):
            task = asyncio.create_task(m.music_metadata(self.obj['path']))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                workers = [worker for worker in asyncio.all_tasks()
                           if worker.get_name() == 'bounded-music-tag-read']
                self.assertEqual(len(workers), 1)
                task.cancel()
                workers[0].cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)

    async def test_queued_reader_cancellation_closes_its_duplicate_descriptor(self):
        import os
        reader = self.path.open('rb')
        duplicates = []
        duplicate = os.dup
        def track(descriptor):
            result = duplicate(descriptor)
            duplicates.append(result)
            return result
        async def queued(_work):
            await asyncio.Event().wait()
        try:
            with patch.object(m.os, 'dup', track), patch.object(m.asyncio, 'to_thread', queued):
                task = asyncio.create_task(m._joined_read(reader.fileno(), self.path.stat().st_size))
                await asyncio.sleep(0)
                workers = [worker for worker in asyncio.all_tasks()
                           if worker.get_name() == 'bounded-music-tag-read']
                self.assertEqual(len(workers), 1)
                workers[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            self.assertEqual(len(duplicates), 1)
            with self.assertRaises(OSError):
                os.fstat(duplicates[0])
            self.assertTrue(os.fstat(reader.fileno()).st_size)
        finally:
            reader.close()

    async def test_remote_ranges_are_authenticated_bounded_and_identity_checked(self):
        data = fixture()
        row = {'owner_id': 'b' * 32, 'object_id': 'c' * 64, 'resource_id': 'a' * 64,
               'payload': {'size': len(data)}}
        relation = {'credential': 'sealed', 'relationship_id': 'd' * 32, 'peer_endpoint': 'https://storage.4399.com'}
        credential, requests = p.encode(bytes(32)), []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield self.value
        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.query, b'')
            token = p.verify_media_token(credential, request.headers['x-media-capability'], int(m.time.time()))
            self.assertEqual(token['g'], row['resource_id'])
            start, end = map(int, request.headers['range'].removeprefix('bytes=').split('-'))
            stream = Stream();stream.value = data[start:end+1]
            return httpx.Response(206, headers={'Content-Range': f'bytes {start}-{end}/{len(data)}',
                                  'X-Media-Resource-ID': row['resource_id'], 'X-Media-Owner-ID': row['owner_id'],
                                  'X-Media-Object-ID': row['object_id']}, stream=stream)
        original = httpx.AsyncClient
        def client(**kwargs):
            self.assertIsInstance(kwargs['verify'], ssl.SSLContext)
            self.assertEqual(kwargs['verify'].verify_mode, ssl.CERT_REQUIRED)
            self.assertFalse(kwargs['trust_env'])
            self.assertFalse(kwargs['follow_redirects'])
            return original(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(state, 'node', {'node_id': 'e' * 32}), patch.object(state, 'unseal', return_value=credential), patch.object(m.httpx, 'AsyncClient', client):
            result = await m._remote_tags(row, relation)
        self.assertEqual(result.picture, JPEG)
        self.assertEqual([r.headers['range'] for r in requests], ['bytes=0-9', f'bytes=10-{len(data)-1}'])

    async def test_redirects_and_wrong_range_proofs_are_rejected_before_body_reads(self):
        row = {'owner_id': 'b' * 32, 'object_id': 'c' * 64, 'resource_id': 'a' * 64,
               'payload': {'size': 1000}}
        relation = {'credential': 'sealed', 'relationship_id': 'd' * 32, 'peer_endpoint': 'https://storage.4399.com'}
        class ForbiddenBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise AssertionError('unbound body read')
                yield b''
        original = httpx.AsyncClient
        for status in (200, 206, 307):
            with self.subTest(status=status):
                def handler(_request):
                    return httpx.Response(status, headers={'Location': 'https://foreign.4399.com'}, stream=ForbiddenBody())
                def client(**kwargs):
                    return original(transport=httpx.MockTransport(handler), **kwargs)
                with patch.object(state, 'node', {'node_id': 'e' * 32}), patch.object(state, 'unseal', return_value=p.encode(bytes(32))), patch.object(m.httpx, 'AsyncClient', client):
                    with self.assertRaises(ValueError):
                        await m._remote_tags(row, relation)
