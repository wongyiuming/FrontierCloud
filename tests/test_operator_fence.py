"""Operator fencing on real authenticated routes, native bytes and trusted TLS."""
import asyncio
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

import httpx
from fastapi import FastAPI
from starlette.responses import Response

from app.middleware.operator_fence import OperatorFenceMiddleware
from app.services import resource_pool, site_control, upload_cleanup
from app.services.federation import runtime as control_runtime


class OperatorFenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.hard = self.root / "hard"
        self.manual = self.root / "manual"
        self.patches = [patch.object(site_control, "NATIVE_MAINTENANCE", self.hard),
                        patch.object(site_control, "MANUAL_MAINTENANCE", self.manual)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    async def test_hard_fence_blocks_all_business_methods_before_body_or_handler(self):
        calls = []
        app = FastAPI()
        app.add_middleware(OperatorFenceMiddleware)

        @app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
        async def handler(path: str):
            calls.append(path)
            return Response(b'ordinary business bytes')

        self.hard.touch()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://4399.com') as client:
            for path in ('/', '/static/app.js', '/api/v1/media/admin/nodes', '/internal/v1/backup/begin',
                         '/internal/v1/storage/' + 'a' * 64, '/internal/v1/certificate/status'):
                for method in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
                    result = await client.request(method, path, headers={'Range': 'bytes=0-3'})
                    self.assertEqual(result.status_code, 503, (method, path))
                    self.assertEqual(result.headers['cache-control'], 'no-store')
                    self.assertEqual(result.headers['retry-after'], '30')
                    self.assertNotIn('x-accel-redirect', result.headers)
                    if method == 'HEAD':
                        self.assertEqual(result.content, b'')
            self.assertEqual(calls, [])
            self.hard.unlink()
            self.assertEqual((await client.get('/')).content, b'ordinary business bytes')

    async def test_exact_read_only_probe_and_challenge_allowlist(self):
        app = FastAPI()
        app.add_middleware(OperatorFenceMiddleware)

        @app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST'])
        async def handler(path: str):
            return Response(b'probe')

        self.hard.touch()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://4399.com') as client:
            for path in ('/health', '/health/live', '/health/ready', '/api/v1/health', '/metrics'):
                self.assertEqual((await client.get(path)).status_code, 200)
                self.assertEqual((await client.head(path)).status_code, 200)
                self.assertEqual((await client.post(path)).status_code, 503)
            good = '/internal/v1/identity?challenge=' + 'a' * 32
            self.assertEqual((await client.get(good)).status_code, 200)
            for path in ('/health/writer', '/health/ready/extra', '/internal/v1/identity',
                         good + '&challenge=' + 'b' * 32, good + '&action=write',
                         '/internal/v1/identity?challenge=' + 'A' * 32,
                         '/internal/v1/identity?challenge=' + 'a' * 31,
                         '/internal/v1/identity/extra?challenge=' + 'a' * 32):
                self.assertEqual((await client.get(path)).status_code, 503, path)
            self.assertEqual((await client.head(good)).status_code, 503)
            self.assertEqual((await client.post(good)).status_code, 503)

    async def test_manual_flag_does_not_activate_operator_fence(self):
        self.manual.touch()
        app = FastAPI()
        app.add_middleware(OperatorFenceMiddleware)

        @app.post('/internal/v1/heartbeat')
        async def handler():
            return Response(b'existing manual maintenance behavior')

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://4399.com') as client:
            self.assertEqual((await client.post('/internal/v1/heartbeat')).status_code, 200)
        self.assertFalse(site_control.hard_fence_active())

    async def test_master_and_native_storage_use_the_outermost_gate(self):
        import main
        self.assertIs(main.app.user_middleware[0].cls, OperatorFenceMiddleware)
        self.hard.touch()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://4399.com') as client:
            for path in ('/api/v1/media/admin/', '/internal/v1/backup/begin', '/api/v1/media/'):
                self.assertEqual((await client.get(path)).status_code, 503)
            self.assertEqual((await client.get('/health/live')).status_code, 200)

    async def test_websocket_does_not_enter_business_handler_under_fence(self):
        self.hard.touch()
        business = AsyncMock()
        sent = []
        async def send(message):
            sent.append(message)
        await OperatorFenceMiddleware(business)({'type': 'websocket', 'path': '/business'}, AsyncMock(), send)
        business.assert_not_awaited()
        self.assertEqual(sent, [{'type': 'websocket.close', 'code': 1013, 'reason': 'Operator maintenance'}])

    async def test_runtime_tick_and_backup_pause_and_resume(self):
        unit = control_runtime.Runtime()
        relation = {'relationship_id': 'a' * 32, 'state': 'revoked', 'summary': {}}
        unit.notify_revocation = AsyncMock()
        unit._run_backup = AsyncMock()
        self.hard.touch()
        await unit.tick(relation)
        unit.schedule_backup(relation)
        with patch.object(resource_pool, 'list_members', new=AsyncMock(return_value=[])) as members:
            await unit.maybe_backup(relation)
            members.assert_not_awaited()
            self.hard.unlink()
            await unit.maybe_backup(relation)
            members.assert_awaited_once()
        unit.notify_revocation.assert_not_awaited()
        self.assertEqual(unit.backup_tasks, {})
        await unit.tick(relation)
        unit.notify_revocation.assert_awaited_once_with(relation)
        unit.schedule_backup(relation)
        await unit.backup_tasks[relation['relationship_id']]
        unit._run_backup.assert_awaited_once_with(relation)

    async def test_runtime_control_cycle_does_not_retire_or_delete_under_fence(self):
        unit = control_runtime.Runtime()
        state = SimpleNamespace(node={'role': 'Master'}, database=object(),
                                cleanup_playback_events=AsyncMock(), list_relationships=AsyncMock(return_value=[]))
        self.hard.touch()
        with patch.object(control_runtime, 'state', state), \
             patch.object(control_runtime.transport, 'close', new=AsyncMock()), \
             patch.object(resource_pool, 'retry_pending_deletes', new=AsyncMock()) as deletion:
            task = asyncio.create_task(unit.run())
            try:
                await asyncio.sleep(0.02)
                state.cleanup_playback_events.assert_not_awaited()
                state.list_relationships.assert_not_awaited()
                deletion.assert_not_awaited()
                self.hard.unlink()
                unit.wakeup.set()
                for _ in range(100):
                    if deletion.await_count:
                        break
                    await asyncio.sleep(0.01)
                state.cleanup_playback_events.assert_awaited_once()
                deletion.assert_awaited_once_with(state)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_upload_cleanup_loop_does_not_release_reservations_under_fence(self):
        self.hard.touch()
        turns = 0
        async def advance(_seconds):
            nonlocal turns
            turns += 1
            if turns == 2:
                self.hard.unlink()
            if turns == 3:
                raise asyncio.CancelledError()
        with patch.object(upload_cleanup.asyncio, 'sleep', new=advance), \
             patch.object(resource_pool, 'release_expired_uploads', new=AsyncMock(return_value=0)) as release, \
             patch.object(upload_cleanup, 'cleanup_stale_upload_parts', new=Mock(return_value=(0, 0))) as parts:
            with self.assertRaises(asyncio.CancelledError):
                await upload_cleanup.run_stale_upload_cleanup()
            release.assert_awaited_once()
            parts.assert_called_once()

    @unittest.skipIf(os.name == 'nt', 'Production upload claims use Linux flock')
    async def test_startup_part_cleanup_preserves_bytes_until_fence_is_removed(self):
        part = self.root / '.upload-abandoned.part'
        part.write_bytes(b'abandoned upload bytes')
        os.utime(part, (1, 1))
        self.hard.touch()
        self.assertEqual(upload_cleanup.cleanup_stale_upload_parts(self.root, 1), (0, 0))
        self.assertTrue(part.exists())
        self.hard.unlink()
        self.assertEqual(upload_cleanup.cleanup_stale_upload_parts(self.root, 1), (1, len(b'abandoned upload bytes')))
        self.assertFalse(part.exists())


@unittest.skipIf(os.name == 'nt', 'Native storage product runs in Linux Compose')
class OperatorFenceStorageRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.test_upload_recovery_runtime import UploadRecoveryRuntime
        from app.api import internal_nodes, internal_storage_integrity
        from app.middleware import native_storage
        self.fixture = UploadRecoveryRuntime()
        await self.fixture.asyncSetUp()
        self.hard = self.fixture.root / 'hard'
        self.patches = [patch.object(site_control, 'NATIVE_MAINTENANCE', self.hard),
                        patch.object(native_storage.settings, 'DEPLOYMENT_MODE', 'only_stroge'),
                        patch.object(native_storage, 'state', self.fixture.follower),
                        patch('app.api.v1.media.MEDIA_ROOT', self.fixture.follower_root),
                        patch('app.api.v1.media.engine', self.fixture.follower.database),
                        patch('app.api.v1.media.node_state', self.fixture.follower)]
        for item in self.patches:
            item.start()
        self.app = FastAPI()
        self.app.include_router(internal_storage_integrity.router, prefix='/internal/v1')
        self.app.include_router(internal_nodes.router)
        self.app.add_middleware(native_storage.NativeStorageDeliveryMiddleware)
        self.app.add_middleware(OperatorFenceMiddleware)
        @self.app.get('/health/live')
        async def healthy():
            return {'status': 'healthy'}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://storage.4399.com')

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        await self.fixture.asyncTearDown()

    async def test_valid_signed_storage_write_and_plain_range_are_fenced_then_resume(self):
        from app.services.federation import protocol as p
        from sqlalchemy import text
        original = 'a' * 64
        path = '/internal/v1/storage/' + original
        capability = self.fixture.token(original)
        p.verify_storage_token(self.fixture.credential, capability, int(time.time()))
        self.hard.touch()
        response = await self.client.put(path, content=self.fixture.data, headers={'X-Storage-Capability': capability})
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.fixture.follower_root / self.fixture.path).exists())
        async with self.fixture.follower.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM cluster_upload_sessions')), 0)
        self.hard.unlink()
        response = await self.client.put(path, content=self.fixture.data, headers={'X-Storage-Capability': capability})
        self.assertEqual(response.status_code, 200, response.text)
        media_token = p.media_token(self.fixture.credential, self.fixture.relationship, self.fixture.master.node['node_id'],
                                    self.fixture.follower.node['node_id'], original, original, int(time.time()))
        media = f'/internal/v1/media/{original}?token={media_token}'
        partial = await self.client.get(media, headers={'Range': 'bytes=3-11'})
        self.assertEqual(partial.status_code, 206, partial.text)
        self.assertEqual(partial.content, self.fixture.data[3:12])
        self.hard.touch()
        for method in ('GET', 'HEAD'):
            response = await self.client.request(method, media, headers={'Range': 'bytes=3-11'})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('content-range', response.headers)
        control_path = path + '/delete'
        body = p.canonical({'path': self.fixture.path, 'expected_bytes': len(self.fixture.data)})
        signed = p.auth_headers(self.fixture.credential, self.fixture.relationship, 'POST', control_path, body)
        relation = await self.fixture.follower.authenticate(httpx.Headers(signed), 'POST', control_path, body)
        self.assertEqual(relation['relationship_id'], self.fixture.relationship)
        blocked = await self.client.post(control_path, content=body, headers=signed)
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual((self.fixture.follower_root / self.fixture.path).read_bytes(), self.fixture.data)
        self.hard.unlink()
        self.assertEqual((await self.client.head(media)).status_code, 200)

    async def test_real_trusted_tls_identity_and_health_survive_the_fence(self):
        from cryptography.hazmat.primitives import serialization
        from tests.certificate_fixtures import TestCA
        from app.services.federation import protocol as p
        from app.services.federation.transport import Transport
        import uvicorn
        ca = TestCA()
        host = 'storage.4399.com'
        certificate, key = ca.pair(host)
        certfile, keyfile = self.fixture.root / 'certificate.pem', self.fixture.root / 'private.pem'
        certfile.write_bytes(certificate)
        keyfile.write_bytes(key)
        os.chmod(keyfile, 0o600)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        origin = f'https://{host}:{listener.getsockname()[1]}'
        self.hard.touch()
        previous = self.fixture.follower.node['endpoint']
        self.fixture.follower.node['endpoint'] = origin
        config = uvicorn.Config(self.app, host='127.0.0.1', lifespan='off', proxy_headers=False,
                                access_log=False, log_level='error',
                                ssl_certfile=str(certfile), ssl_keyfile=str(keyfile))
        server = uvicorn.Server(config)
        worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        worker.start()
        context = ssl.create_default_context(cadata=ca.root.public_bytes(serialization.Encoding.PEM).decode())
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        transport = Transport()
        lookup = socket.getaddrinfo
        def local_dns(value, port, *args, **kwargs):
            return lookup('127.0.0.1' if value in (host, host.encode()) else value, port, *args, **kwargs)
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(server.started)
            with patch('app.services.federation.transport.ssl.create_default_context', return_value=context):
                transport.open()
            with patch.object(socket, 'getaddrinfo', new=local_dns):
                identity = await transport.identity(origin, expected_id=self.fixture.follower.node['node_id'],
                    expected_key=p.public_key(self.fixture.follower.unseal(self.fixture.follower.node['private_key'])), role='Follower')
                self.assertEqual(identity['endpoint'], origin)
                async with httpx.AsyncClient(verify=context, trust_env=False) as client:
                    self.assertEqual((await client.get(origin + '/health/live')).status_code, 200)
                    self.assertEqual((await client.put(origin + '/internal/v1/storage/' + 'b' * 64,
                        content=self.fixture.data, headers={'X-Storage-Capability': self.fixture.token('b' * 64)})).status_code, 503)
                    self.assertEqual((await client.get(origin + '/internal/v1/media/' + 'b' * 64,
                        headers={'Range': 'bytes=0-3'})).status_code, 503)
                    self.assertEqual((await client.head(origin + '/internal/v1/media/' + 'b' * 64)).status_code, 503)
                    self.assertEqual((await client.get(origin + '/internal/v1/identity?challenge=' + uuid.uuid4().hex,
                        headers={'X-Forwarded-Proto': 'http'})).status_code, 200)
                async with httpx.AsyncClient(trust_env=False) as untrusted:
                    with self.assertRaises(httpx.ConnectError):
                        await untrusted.get(origin + '/health/live')
        finally:
            await transport.close()
            server.should_exit = True
            await asyncio.to_thread(worker.join, 5)
            listener.close()
            self.fixture.follower.node['endpoint'] = previous
            self.assertFalse(worker.is_alive())
