"""Real signed Follower uploads persist all directory entries before SQL completion."""
import asyncio
import base64
import json
import os
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import patch

from sqlalchemy import text

from app.api import internal_storage_integrity as storage
from tests import test_upload_recovery_runtime as fixtures


# A separate writer exits at the last required directory fsync: no exception
# handler or Python finally block can reconcile the durable reservation for it.
_CRASH_WRITER = r'''
import asyncio, base64, json, os, sys
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi import FastAPI
from sqlalchemy.engine import URL
from app.api import internal_nodes, internal_storage_integrity as storage
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_manager, upload_claims
from app.services.federation.state import State

async def run(value):
    root = Path(value['media'])
    database = create_sqlite_engine(URL.create('sqlite+aiosqlite', database=value['database']))
    state = State(database, value['vault'].encode())
    await state.initialize()
    sync = os.fsync
    def crash(descriptor):
        if Path(os.readlink('/proc/self/fd/' + str(descriptor))) == root:
            os._exit(73)
        sync(descriptor)
    with patch.object(storage, 'state', state), patch.object(internal_nodes, 'state', state), \
         patch.object(storage, 'MEDIA_ROOT', root), patch.object(media_manager, 'MEDIA_ROOT', root), \
         patch.object(internal_nodes.settings, 'TLS_ENABLED', True), \
         patch.object(upload_claims, 'ROOT', Path(value['leases'])), patch.object(os, 'fsync', crash):
        app = FastAPI(); app.include_router(storage.router, prefix='/internal/v1')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://storage.4399.com') as client:
            response = await client.put('/internal/v1/storage/' + value['original'],
                content=base64.b64decode(value['data']),
                headers={'X-Storage-Capability': value['token']})
            raise RuntimeError('Crash boundary was not reached: HTTP ' + str(response.status_code))
asyncio.run(run(json.load(sys.stdin)))
'''


@unittest.skipIf(os.name == 'nt', 'Product durability and upload leases use Linux filesystems')
class FollowerUploadDurabilityTests(unittest.IsolatedAsyncioTestCase):
    token = fixtures.UploadRecoveryRuntime.token
    counts = fixtures.UploadRecoveryRuntime.counts
    age = fixtures.UploadRecoveryRuntime.age

    async def asyncSetUp(self):
        await fixtures.UploadRecoveryRuntime.asyncSetUp(self)
        self.path = 'music/NewCategory/Album/song.mp3'

    async def asyncTearDown(self):
        await fixtures.UploadRecoveryRuntime.asyncTearDown(self)

    def sync_observer(self, events, *, fail=None):
        sync, failed = os.fsync, False
        def observe(descriptor):
            nonlocal failed
            target = Path(os.readlink('/proc/self/fd/' + str(descriptor)))
            if target == fail and not failed:
                failed = True
                raise OSError('Injected required directory durability failure')
            sync(descriptor)
            events.append(target)
        return observe

    def hierarchy(self):
        parent = (self.follower_root / self.path).parent
        return [parent, parent.parent, self.follower_root / 'music', self.follower_root]

    async def facts(self, original):
        async with self.follower.database.connect() as conn:
            owner = await conn.scalar(text('SELECT media_id FROM media_objects WHERE media_id=:id'), {'id': original})
        return (await storage._session(original))['state'], owner, await self.counts(self.follower.database, self.follower.node['node_id'])

    async def upload(self, original, *, encrypted=False):
        return await self.client.put('/internal/v1/storage/' + original, content=self.data,
            headers={'X-Storage-Capability': self.token(original, encrypted=encrypted)})

    async def assert_durable_upload(self, *, encrypted=False):
        original = secrets.token_hex(32)
        self.assertFalse((self.follower_root / 'music/NewCategory').exists())
        events, complete = [], storage._complete
        async def finalize(*args):
            self.assertEqual(events, [self.follower_root / ('.cluster-upload-' + original + '.part'), *self.hierarchy()])
            self.assertEqual(await self.facts(original), ('reserved', None, (0, len(self.data))))
            self.assertEqual((self.follower_root / self.path).read_bytes(), self.data)
            self.assertFalse(events[0].exists())
            return await complete(*args)
        with patch.object(os, 'fsync', self.sync_observer(events)), patch.object(storage, '_complete', finalize):
            response = await self.upload(original, encrypted=encrypted)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['state'], 'complete')
        self.assertEqual(await self.facts(original), ('complete', original, (len(self.data), 0)))

    async def test_plaintext_new_two_parent_levels_and_root_sync_precede_real_completion(self):
        await self.assert_durable_upload()

    async def test_opaque_encrypted_upload_has_same_directory_durability(self):
        self.data = b'opaque browser ciphertext without a plaintext signature' * 20
        await self.assert_durable_upload(encrypted=True)

    async def test_required_parent_or_root_sync_error_cannot_commit_or_consume_used_quota(self):
        for index, boundary in enumerate(('category', 'root')):
            with self.subTest(boundary=boundary):
                self.path = 'music/NewCategory' + str(index) + '/Album/song.mp3'
                original, events = secrets.token_hex(32), []
                fail = (self.follower_root / self.path).parent.parent if boundary == 'category' else self.follower_root
                complete = storage._complete
                with patch.object(os, 'fsync', self.sync_observer(events, fail=fail)), patch.object(storage, '_complete', wraps=complete) as commit:
                    with self.assertRaisesRegex(OSError, 'required directory durability'):
                        await self.upload(original)
                commit.assert_not_called()
                self.assertEqual(await self.facts(original), ('cancelled', None, (0, 0)))
                self.assertFalse((self.follower_root / self.path).exists())

    async def reserve_crashed_publication(self):
        original = secrets.token_hex(32)
        await storage._reserve(original, self.path, len(self.data))
        target = self.follower_root / self.path
        target.parent.mkdir(parents=True)
        target.write_bytes(self.data)
        return original, target

    async def test_recovered_reserved_file_and_every_parent_sync_precede_completion(self):
        original, target = await self.reserve_crashed_publication()
        events, complete = [], storage._complete
        async def finalize(*args):
            self.assertEqual(events, [target, *self.hierarchy()])
            self.assertEqual(await self.facts(original), ('reserved', None, (0, len(self.data))))
            return await complete(*args)
        with patch.object(os, 'fsync', self.sync_observer(events)), patch.object(storage, '_complete', finalize):
            result = await storage._proof(original, self.path, len(self.data))
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(await self.facts(original), ('complete', original, (len(self.data), 0)))

    async def test_recovery_sync_failure_keeps_bytes_reserved_until_durable_retry(self):
        original, target = await self.reserve_crashed_publication()
        for boundary in (target, self.follower_root):
            events = []
            with patch.object(os, 'fsync', self.sync_observer(events, fail=boundary)), patch.object(storage, '_complete', wraps=storage._complete) as commit:
                with self.assertRaises(OSError):
                    await storage._proof(original, self.path, len(self.data))
            commit.assert_not_called()
            self.assertEqual(target.read_bytes(), self.data)
            self.assertEqual(await self.facts(original), ('reserved', None, (0, len(self.data))))
        self.assertEqual((await storage._proof(original, self.path, len(self.data)))['state'], 'complete')
        self.assertEqual(await self.facts(original), ('complete', original, (len(self.data), 0)))

    async def test_actual_writer_exit_before_root_sync_never_completes_and_recovery_persists_chain(self):
        original = secrets.token_hex(32)
        value = {'media': str(self.follower_root), 'database': str(self.root / 'follower/state.db'),
            'leases': str(self.root / 'leases'), 'original': original, 'data': base64.b64encode(self.data).decode(),
            'vault': base64.urlsafe_b64encode(self.follower._vault._signing_key + self.follower._vault._encryption_key).decode(),
            'token': self.token(original)}
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', _CRASH_WRITER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(json.dumps(value).encode()), 20)
        except BaseException:
            if process.returncode is None:
                process.kill(); await process.wait()
            raise
        self.assertEqual(process.returncode, 73, (output + error).decode())
        self.assertEqual(await self.facts(original), ('reserved', None, (0, len(self.data))))
        self.assertEqual((self.follower_root / self.path).read_bytes(), self.data)
        self.assertFalse((self.follower_root / ('.cluster-upload-' + original + '.part')).exists())
        await self.age(self.follower.database, storage.session_id(original))
        events = []
        with patch.object(os, 'fsync', self.sync_observer(events)):
            self.assertEqual(await storage.cleanup_expired_storage(), 1)
        self.assertEqual(events, [self.follower_root / self.path, *self.hierarchy()])
        self.assertEqual(await self.facts(original), ('complete', original, (len(self.data), 0)))


if __name__ == '__main__':
    unittest.main()
