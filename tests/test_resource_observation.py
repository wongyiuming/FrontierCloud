"""Actual SQLite/files and authenticated, signed content-observation controls."""
import asyncio
import hashlib
import json
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text, update

from app.api import internal_resource_observation as api
from app.services import media_crypto as crypto, media_manager, recording_crypto
from app.services.business_traffic import Monitor
from app.services.federation import protocol as p, schema as fs
from app.services.resource_observation import CHUNK_BYTES, LIFETIME, ContentObserver, ObservationError, digest
from app.services.resource_observation_client import ObservationClient
from tests import test_resource_inventory_api as fixture


@unittest.skipUnless(os.name == 'posix', 'native content observation uses product Linux openat')
class ContentObservationTests(unittest.IsolatedAsyncioTestCase):
    verified = fixture.InventoryAPITests.verified

    async def asyncSetUp(self):
        await fixture.InventoryAPITests.asyncSetUp(self)
        self.now = 1000.0
        self.traffic = Monitor(monotonic=lambda: self.now, wall=lambda: self.now)
        self.now += 30
        self.observer = ContentObserver(self.store, root_provider=lambda: {'media': self.files,
            'recordings': self.root/'recordings'}, traffic=self.traffic, clock=lambda: self.now)
        self.patches.extend([patch.object(api, 'observer', self.observer), patch.object(media_manager, 'MEDIA_ROOT', self.files)])
        self.patches[-2].start()
        self.patches[-1].start()
        self.client._transport.app.include_router(api.router)
        self.identifier = 'e'*64

    async def asyncTearDown(self):
        await self.observer.stop()
        await fixture.InventoryAPITests.asyncTearDown(self)

    async def request(self, action, value):
        path = '/internal/v1/migration/observe/' + action
        body = p.canonical(value)
        return await self.client.post(path, content=body,
            headers=p.auth_headers(self.credential, self.relationship, 'POST', path, body))

    async def register(self, data, *, metadata=None):
        self.path = self.files/'music/历史/song.mp3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)
        path = 'music/历史/song.mp3'
        async with self.database.begin() as conn:
            await conn.execute(text('INSERT INTO media_objects VALUES (:id,:kind,:path,:locator,:created,:updated)'),
                dict(id=self.identifier, kind='audio', path=path, locator=hashlib.sha256(path.encode()).hexdigest(),
                     created='2026-01-02 03:04:05.123456', updated='2026-02-03 04:05:06.654321'))
            if metadata is not None:
                await crypto.put_encryption(conn, self.identifier, metadata)

    async def start(self, *, request_id='d'*32, kind='media', object_id=None, user_id=''):
        return self.verified(await self.request('start', dict(request_id=request_id, resource_kind=kind,
            object_id=self.identifier if object_id is None else object_id, user_id=user_id)))

    async def completed(self):
        await asyncio.wait_for(asyncio.shield(self.observer.current.task), 5)
        record = self.observer.current
        return self.verified(await self.request('status', {'observation_id': record.header['observation_id']}))

    async def test_plain_bytes_are_incremental_and_only_completed_results_contain_hashes(self):
        data = b'ID3' + bytes(2*1024**2)
        await self.register(data)
        with patch.object(self.observer, '_authorize', wraps=self.observer._authorize) as auth:
            first = await self.start()
            self.assertNotIn('result', first)
            result = await self.completed()
            self.assertEqual(result['state'], 'completed')
            self.assertGreaterEqual(auth.await_count, len(data)//CHUNK_BYTES)
        self.assertEqual(result['result']['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(result['result']['bytes'], len(data))
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual(result['result']['source_version'], digest({key: result['result'][key] for key in
            ('physical_path', 'bytes', 'metadata_sha256', 'encryption_sha256', 'native')}))

    async def test_actual_ciphertext_is_hashed_without_opening_premaster_or_deriving_keys(self):
        clear = b'private media' * 8000
        meta = crypto.Metadata.new(len(clear))
        cipher = AESGCM(bytes(32)).encrypt(meta.chunk_nonce(0), clear, meta.chunk_aad(0))
        await self.register(cipher, metadata=meta)
        with (patch.object(crypto, 'open_premaster', side_effect=AssertionError('media key opened')),
              patch.object(crypto.Manager, 'file_key', side_effect=AssertionError('media key derived'))):
            await self.start()
            result = await self.completed()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['result']['sha256'], hashlib.sha256(cipher).hexdigest())
        self.assertNotIn('nonce_prefix', json.dumps(result))
        self.assertNotIn('private media', json.dumps(result))
        self.assertEqual(self.path.read_bytes(), cipher)

    async def test_recording_plain_legacy_and_opaque_encrypted_footer(self):
        user, recording = '1'*32, '2'*32
        path = self.root/'recordings'/self.relationship/user/(recording+'.bin')
        path.parent.mkdir(parents=True)
        descriptor = crypto.Metadata.new(3)
        snapshot = {'encryption': descriptor.to_dict(), 'ciphertext': crypto._encode64(bytes(descriptor.ciphertext_size))}
        for index, footer in enumerate((None, {'version': 1, 'title': 'recording', 'lyrics': []},
                                       {'version': 1, 'title': 'recording', 'lyrics': [], 'encrypted_lyrics': snapshot})):
            data = b'RIFF-original-audio'
            if footer is not None:
                raw = p.canonical(footer)
                data += raw + len(raw).to_bytes(8, 'big') + recording_crypto.TRAILER_MAGIC
            path.write_bytes(data)
            await self.start(request_id=str(index+3)*32, kind='recording', object_id=recording, user_id=user)
            result = await self.completed()
            self.assertEqual(result['state'], 'completed', result)
            self.assertEqual(result['result']['sha256'], hashlib.sha256(data).hexdigest())
            self.assertNotIn('ciphertext', json.dumps(result))
            self.assertEqual(path.read_bytes(), data)

    async def test_invalid_or_oversized_recording_footer_is_not_silently_accepted(self):
        user, recording = '1'*32, '2'*32
        path = self.root/'recordings'/self.relationship/user/(recording+'.bin')
        path.parent.mkdir(parents=True)
        for index, data in enumerate((b'x'+(3*1024**2).to_bytes(8, 'big')+recording_crypto.TRAILER_MAGIC,
                b'x'+b'{}'+(2).to_bytes(8, 'big')+recording_crypto.TRAILER_MAGIC)):
            path.write_bytes(data)
            await self.start(request_id=str(index+3)*32, kind='recording', object_id=recording, user_id=user)
            result = await self.completed()
            self.assertEqual(result['state'], 'failed')
            self.assertNotIn('result', result)
            self.assertEqual(path.read_bytes(), data)

    async def test_unregistered_media_never_opens_a_file(self):
        with patch('app.middleware.native_storage._open_beneath', side_effect=AssertionError('unregistered open')):
            await self.start()
            result = await self.completed()
        self.assertEqual(result['error_code'], 'observation_media_unregistered')

    async def test_same_size_content_change_and_metadata_change_invalidate_proof(self):
        for index, change in enumerate(('bytes', 'metadata')):
            if index == 0:
                await self.register(bytes(CHUNK_BYTES*3))
            original = self.observer._quiet
            changed = False
            async def mutate(record):
                nonlocal changed
                await original(record)
                if record.observed_bytes and not changed:
                    changed = True
                    if change == 'bytes':
                        self.path.write_bytes(b'x'*(CHUNK_BYTES*3))
                    else:
                        async with self.database.begin() as conn:
                            await conn.execute(text("UPDATE media_objects SET updated_at='2026-03-04 05:06:07.123456'"))
            with patch.object(self.observer, '_quiet', mutate):
                await self.start(request_id=str(index+3)*32)
                result = await self.completed()
            self.assertEqual(result['state'], 'failed', result)
            self.assertEqual(result['error_code'], 'observation_resource_changed')
            self.assertNotIn('result', result)

    async def test_revocation_stops_between_chunks_and_exposes_no_result(self):
        await self.register(bytes(CHUNK_BYTES*3))
        original = self.observer._quiet
        async def revoke(record):
            if record.observed_bytes:
                async with self.database.begin() as conn:
                    await conn.execute(update(fs.relationships).values(state='revoked'))
            await original(record)
        with patch.object(self.observer, '_quiet', revoke):
            await self.start()
            await asyncio.wait_for(asyncio.shield(self.observer.current.task), 3)
        record = self.observer.current
        self.assertEqual(record.error_code, 'observation_relationship_changed')
        self.assertEqual(record.observed_bytes, CHUNK_BYTES)
        response = await self.request('status', {'observation_id': record.header['observation_id']})
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(record.result)

    async def test_high_traffic_retains_fixed_deadline_and_does_not_open_file(self):
        await self.register(b'original')
        self.traffic.record(egress=262144)
        with patch('app.middleware.native_storage._open_beneath', side_effect=AssertionError('opened during high traffic')):
            first = await self.start()
            await asyncio.sleep(.03)
            self.assertEqual(self.observer.current.state, 'waiting_low_traffic')
            again = await self.start()
            self.assertEqual(first['expires_at'], again['expires_at'])
            response = await self.request('start', dict(request_id='f'*32, resource_kind='media',
                object_id=self.identifier, user_id=''))
            self.assertEqual(response.status_code, 409)
            async with asyncio.timeout(.2):
                async with media_manager.media_mutation_lock:
                    pass
            self.now += LIFETIME + 1
            expired = await self.request('status', {'observation_id': first['observation_id']})
            self.assertEqual(expired.status_code, 409)
        self.assertIsNone(self.observer.current)
        self.assertEqual(self.path.read_bytes(), b'original')

    async def test_cancel_joins_inflight_file_read_before_closing_descriptor(self):
        await self.register(bytes(CHUNK_BYTES*2))
        entered, release = threading.Event(), threading.Event()
        original = os.pread
        descriptors = []
        def blocked(descriptor, length, offset):
            descriptors.append(descriptor)
            entered.set()
            if not release.wait(3):
                raise AssertionError('test did not release reader')
            return original(descriptor, length, offset)
        with patch('app.services.resource_observation.os.pread', blocked):
            first = await self.start()
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            closing = asyncio.create_task(self.request('close', {'observation_id': first['observation_id']}))
            try:
                await asyncio.sleep(.02)
                self.assertFalse(closing.done())
                self.assertTrue(os.fstat(descriptors[0]).st_size)
            finally:
                release.set()
            self.assertEqual((await asyncio.wait_for(closing, 2)).status_code, 200)
        self.assertIsNone(self.observer.current)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertTrue(self.path.exists())

    async def test_symlinks_hardlinks_and_retired_encryption_fail_closed(self):
        await self.register(b'original')
        outside = self.root/'outside'
        outside.write_bytes(b'original')
        self.path.unlink()
        self.path.symlink_to(outside)
        await self.start(request_id='3'*32)
        self.assertEqual((await self.completed())['state'], 'failed')
        self.path.unlink()
        os.link(outside, self.path)
        await self.start(request_id='4'*32)
        self.assertEqual((await self.completed())['error_code'], 'observation_resource_invalid')
        self.path.unlink()
        self.path.write_bytes(b'original')
        async with self.database.begin() as conn:
            await conn.execute(text('INSERT INTO media_encryption VALUES (:kind,:id,:file,NULL,1)'),
                               dict(kind='media', id=self.identifier, file='a'*32))
        await self.start(request_id='5'*32)
        self.assertEqual((await self.completed())['error_code'], 'observation_media_retired')
        self.assertEqual(outside.read_bytes(), b'original')

    async def test_paths_or_changed_request_cannot_select_arbitrary_resources(self):
        self.traffic.record(egress=262144)
        await self.register(b'original')
        await self.start()
        for invalid in (dict(request_id='d'*32, resource_kind='recording', object_id='a'*32, user_id='b'*32),
                        dict(request_id='e'*32, resource_kind='media', object_id='../etc', user_id=''),
                        dict(request_id='e'*32, resource_kind='media', object_id=self.identifier, user_id='', path='/etc')):
            self.assertEqual((await self.request('start', invalid)).status_code, 409)

    async def test_completed_status_expires_and_stop_joins_background_waits(self):
        await self.register(b'original')
        first = await self.start()
        result = await self.completed()
        self.assertEqual(result['state'], 'completed')
        self.now += LIFETIME + 1
        self.assertEqual((await self.request('status', {'observation_id': first['observation_id']})).status_code, 409)
        self.traffic.record(egress=262144)
        await self.start(request_id='f'*32)
        task = self.observer.current.task
        await self.observer.stop()
        self.assertTrue(task.done())
        self.assertIsNone(self.observer.current)

    async def test_master_client_rejects_signed_wrong_bindings_or_unfinished_hashes(self):
        await self.register(b'original')
        master = SimpleNamespace(node={'node_id': 'c'*32, 'role': 'Master'}, unseal=lambda value: value)
        relation = dict(relationship_id=self.relationship, peer_id=self.store.node['node_id'],
            peer_endpoint='https://storage.4399.com', peer_key=p.public_key(self.store.unseal(self.store.node['private_key'])),
            credential=self.credential, direction='downstream', state='active')
        tamper = None
        async def rpc(origin, path, **kwargs):
            result = self.verified(await self.request(path.rsplit('/', 1)[1], kwargs['value']))
            if tamper is not None:
                result = tamper(path.rsplit('/', 1)[1], json.loads(json.dumps(result)))
            return p.sign(self.store.unseal(self.store.node['private_key']), result)
        client = ObservationClient(master, relation, SimpleNamespace(request=rpc))
        result = await client.read('media', self.identifier)
        self.assertEqual(result['result']['sha256'], hashlib.sha256(b'original').hexdigest())
        self.assertIsNone(self.observer.current)
        for field, wrong in [('node_id', 'a'*32), ('incarnation', 'not-a-node'), ('object_id', 'b'*64),
                             ('observed_bytes', True), ('expires_at', 1), ('state', {})]:
            tamper = lambda action, value: {**value, field: wrong} if action == 'start' else value
            with self.subTest(field=field), self.assertRaises(ObservationError):
                await client.read('media', self.identifier)
            await self.observer.stop()
        tamper = lambda action, value: {**value, 'result': {'sha256': 'f'*64}} if action == 'start' else value
        with self.assertRaises(ObservationError):
            await client.read('media', self.identifier)
