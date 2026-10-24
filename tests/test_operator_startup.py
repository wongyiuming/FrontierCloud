"""Real SQLite/ASGI fenced startup must load existing state without backfill."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import main as application
from app.core import db
from app.core.config import settings
from app.core.sqlite_compat import create_sqlite_engine
from app.services import health, media_crypto, media_manager, offline_upgrade, runtime_gate, site_control, upload_cleanup
from app.services.federation import protocol
from app.services.federation.state import state
import app.services.federation.state as state_module


@unittest.skipIf(os.name == 'nt', 'Product runtime leases require Linux')
class FencedStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='fc-fenced-startup-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'data'
        self.secrets = Path(temporary.name)/'secrets'
        self.media = self.root/'media'
        for directory in (self.root, self.media, self.secrets):
            directory.mkdir()
        self.path = self.root/'frontiercloud.db'
        self.engine = create_sqlite_engine('sqlite+aiosqlite:///' + str(self.path))
        self.addAsyncCleanup(self.engine.dispose)
        replacements = [
            patch.object(db, 'engine', self.engine), patch.object(db, 'SECRET_DIR', self.secrets),
            patch.object(state, 'database', self.engine), patch.object(state, '_vault', None), patch.object(state, 'node', {}),
            patch.object(state_module, 'SECRET_DIR', self.secrets), patch.object(media_crypto, 'manager', None),
            patch.object(media_manager, 'MEDIA_ROOT', self.media), patch.object(runtime_gate, 'DATA_ROOT', self.root),
            patch.object(site_control, 'DATA_ROOT', self.root),
            patch.object(site_control, 'NATIVE_MAINTENANCE', self.root/'.frontiercloud-native-maintenance'),
            patch.object(settings, 'DEPLOYMENT_MODE', ''), patch.object(settings, 'TLS_ENABLED', True),
            patch.object(application, 'initialize_ip_security_cache', AsyncMock()),
            patch.object(application, 'retry_edge_projection', AsyncMock()),
            patch.object(application, 'run_stale_upload_cleanup', AsyncMock()),
            patch.object(application, 'announce_initial_secrets_once', Mock()),
            patch.object(application.node_runtime, 'start', Mock()), patch.object(application.node_runtime, 'stop', AsyncMock()),
            patch.object(application.resource_inventory, 'reap', AsyncMock()),
            patch.object(application.traffic_monitor, 'watch_gateway', AsyncMock()),
            patch.object(application.traffic_monitor, 'require_gateway', Mock()),
            patch.object(health, 'engine', self.engine), patch.object(health.redis_client, 'ping', AsyncMock(return_value=True)),
        ]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        await db.init_db()
        await state.initialize()
        async with self.engine.begin() as connection:
            await connection.execute(text("UPDATE node_identity SET role='Master',endpoint='https://master.example.test'"))
        await media_crypto.initialize()
        self.readonly = create_async_engine(self.engine.url.set(database=self.path.as_uri(), query={'mode': 'ro', 'uri': 'true'}))
        self.addAsyncCleanup(self.readonly.dispose)
        fallback = self.media/'lyrics/default.lrc'
        fallback.parent.mkdir()
        fallback.write_bytes(b'[00:00.00]preserve existing fallback bytes\n')
        payload = self.media/'music/artist/cipher.mp3'
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b'opaque original ciphertext\x00\xff'*512)
        (self.media/'.upload-retained.part').write_bytes(b'preserve unfinished payload')
        (self.root/('.frontiercloud-media-publication-'+'a'*32+'.json')).write_bytes(b'retain pending publication proof')
        site_control.NATIVE_MAINTENANCE.write_bytes(b'owned operator maintenance fixture\n')
        site_control.NATIVE_MAINTENANCE.chmod(0o600)

    def files(self):
        result = {str(path.relative_to(self.root if self.root in path.parents else self.secrets)):
                hashlib.sha256(path.read_bytes()).hexdigest()
                for base in (self.media, self.secrets, self.root/'media-keys')
                for path in base.rglob('*') if path.is_file()}
        result.update({path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.glob('.frontiercloud-media-publication-*')})
        return result

    async def facts(self):
        return await offline_upgrade.inspect_database(self.readonly, self.secrets, self.root/'media-keys')

    async def test_controller_shutdown_failure_still_stops_node_transport_and_closes_database(self):
        from app.services.resource_migration_runtime import runtime
        original=runtime.stop;before,files=await self.facts(),self.files()
        async def failed_after_join():
            await original()
            raise RuntimeError('fixture controller shutdown acknowledgement failure')
        with patch.object(runtime,'stop',side_effect=failed_after_join),patch.object(application,'close_db',AsyncMock(wraps=application.close_db)) as closed:
            with self.assertRaisesRegex(RuntimeError,'shutdown acknowledgement failure'):
                async with application.lifespan(application.app):pass
            application.node_runtime.stop.assert_awaited_once();closed.assert_awaited_once()
        self.assertIsNone(runtime.scheduler.task)
        self.assertEqual(await self.facts(),before);self.assertEqual(self.files(),files)

    async def test_joined_application_restart_changes_preview_epoch_without_changing_identity_or_state(self):
        from app.services.resource_migration_runtime import runtime
        before,files=await self.facts(),self.files();epochs=[]
        for _ in range(2):
            async with application.lifespan(application.app):
                epochs.append(runtime.planner.epoch)
                self.assertIsNotNone(runtime.scheduler.task)
            self.assertTrue(runtime.closed);self.assertIsNone(runtime.scheduler.task)
        self.assertNotEqual(*epochs)
        self.assertEqual(await self.facts(),before);self.assertEqual(self.files(),files)

    async def test_prepared_fenced_startup_preserves_rows_keys_files_and_serves_only_probes(self):
        before, files = await self.facts(), self.files()
        from functools import partial
        with patch.object(application, 'cleanup_stale_upload_parts', partial(upload_cleanup.cleanup_stale_upload_parts, media_root=self.media)):
            async with application.lifespan(application.app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app), base_url='https://master.example.test') as client:
                    ready = await client.get('/health/ready')
                    self.assertEqual(ready.status_code, 200, ready.text)
                    blocked = await client.get('/api/v1/media/')
                    self.assertEqual(blocked.status_code, 503)
                    identity = await client.get('/internal/v1/identity?challenge='+'b'*32)
                    self.assertEqual(identity.status_code, 200, identity.text)
                    payload = protocol.verify(protocol.public_key(state.unseal(state.node['private_key'])), identity.json())
                    self.assertEqual(payload['node_id'], before['node_id'])
                self.assertEqual(await self.facts(), before)
                self.assertEqual(self.files(), files)
                application.initialize_ip_security_cache.assert_awaited_once_with(read_only=True)
                application.announce_initial_secrets_once.assert_not_called()
        self.assertEqual(await self.facts(), before)
        self.assertEqual(self.files(), files)

    async def test_fenced_startup_rejects_old_schema_without_implicit_migration(self):
        async with self.engine.begin() as connection:
            await connection.execute(text('UPDATE frontiercloud_schema SET generation=6'))
        before, files = await self.facts(), self.files()
        with self.assertRaisesRegex(RuntimeError, f'expected={db.SCHEMA_GENERATION}, actual=6'):
            async with application.lifespan(application.app):
                self.fail('fenced startup accepted an unprepared database')
        self.assertEqual(await self.facts(), before)
        self.assertEqual(self.files(), files)

    async def test_fenced_startup_never_creates_a_missing_database_or_key(self):
        await self.engine.dispose()
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            async with application.lifespan(application.app):
                self.fail('missing database was initialized')
        self.assertFalse(self.path.exists())
        self.assertTrue((self.secrets/'node-vault.key').exists())

    async def test_readonly_identity_and_premaster_load_never_regenerate_missing_keys(self):
        vault = self.secrets/'node-vault.key'
        vault.unlink()
        with self.assertRaises(FileNotFoundError):
            await state.initialize(read_only=True)
        self.assertFalse(vault.exists())
        premaster = self.root/'media-keys/media-premaster.key'
        premaster.unlink()
        with self.assertRaises(media_crypto.PremasterError):
            await media_crypto.initialize(read_only=True)
        self.assertFalse(premaster.exists())


class SecurityProjectionFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_startup_hydrates_cache_without_publishing_sql_projection(self):
        from app.services import ip_security
        class Guard:
            async def __aenter__(self): pass
            async def __aexit__(self, *_): pass
        with (patch.object(ip_security, '_security_state_guard', return_value=Guard()),
              patch.object(ip_security, '_hydrate_ip_security_cache', AsyncMock()) as hydrate,
              patch.object(ip_security, '_refresh_edge_projection', AsyncMock()) as publish):
            await ip_security.initialize_ip_security_cache(read_only=True)
        hydrate.assert_awaited_once()
        publish.assert_not_awaited()
