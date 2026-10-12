"""Real persisted role reset must restore browser grants without a restart."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI

from app.api.v1 import admin_nodes, media_crypto as crypto_api
from app.core import db
from app.core.config import settings
from app.core.sqlite_compat import create_sqlite_engine
from app.middleware.node_role import NodeRoleMiddleware
from app.services import lyrics, media_crypto, media_manager, media_objects
from app.services.business_lifecycle import initialize_business_services
from app.services.federation.state import state
from tests.test_media_crypto import browser_public


class BusinessRoleLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        for name in ('music', 'vido', 'lyrics'):
            (self.media / name).mkdir(parents=True)
        self.database = create_sqlite_engine('sqlite+aiosqlite:///' + str(self.root / 'business.sqlite'))
        self.patches = [
            patch.object(db, 'engine', self.database),
            patch.object(db, 'SECRET_DIR', self.root / 'secrets'),
            patch.object(lyrics, 'engine', self.database),
            patch.object(lyrics, 'MEDIA_ROOT', self.media),
            patch.object(lyrics, 'LYRICS_ROOT', self.media / 'lyrics'),
            patch.object(media_manager, 'MEDIA_ROOT', self.media),
            patch.object(state, 'database', self.database),
            patch.object(state, 'node', {}),
            patch.object(state, '_vault', Fernet(Fernet.generate_key())),
            patch.object(media_crypto, 'manager', None),
            patch.object(settings, 'TLS_ENABLED', True),
            patch.object(admin_nodes.runtime, 'stop', AsyncMock()),
            patch.object(admin_nodes.runtime, 'start', Mock()),
            patch('app.services.media_catalog_cache.invalidate_media_catalog', AsyncMock()),
        ]
        for item in self.patches:
            item.start()
        await db.init_db()
        await state.initialize()
        await state.promote('Follower', 'https://storage.4399.com', 'test')
        app = FastAPI()
        app.include_router(admin_nodes.router, prefix='/api/v1/media/admin')
        app.include_router(crypto_api.router)
        app.add_middleware(NodeRoleMiddleware)
        async def administrator():
            return 'test-administrator'
        app.dependency_overrides[admin_nodes.require_session] = administrator
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://storage.4399.com')

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        await self.database.dispose()

    async def reset(self, confirmation=None):
        return await self.client.post('/api/v1/media/admin/nodes/reinitialize',
                                      json={'confirmation': confirmation or state.node['node_id']})

    async def test_follower_startup_never_creates_business_key_or_default_lyric(self):
        await initialize_business_services()
        self.assertIsNone(media_crypto.manager)
        self.assertFalse((self.root / 'media-keys').exists())
        self.assertFalse((self.media / lyrics.DEFAULT_LYRIC_PATH).exists())
        response = await self.client.post('/api/v1/media/crypto/session',
            json={'public_key': browser_public(ec.generate_private_key(ec.SECP256R1()))})
        self.assertEqual(response.status_code, 409)

    async def test_real_empty_follower_reset_initializes_business_before_response(self):
        original = state.node['node_id']
        response = await self.reset()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['role'], 'Standalone')
        self.assertNotEqual(response.json()['node_id'], original)
        self.assertEqual(await media_crypto.business_role(), 'Standalone')
        self.assertIsNotNone(media_crypto.manager)
        self.assertEqual((self.media / lyrics.DEFAULT_LYRIC_PATH).read_text(), lyrics.DEFAULT_LYRIC_CONTENT)
        private = ec.generate_private_key(ec.SECP256R1())
        response = await self.client.post('/api/v1/media/crypto/session', json={'public_key': browser_public(private)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreaterEqual(response.json()['expires_in'], media_crypto.SESSION_LIFETIME - 2)
        self.assertLessEqual(response.json()['expires_in'], media_crypto.SESSION_LIFETIME)
        admin_nodes.runtime.start.assert_called_once_with(revocations=False)

    async def test_wrong_reset_confirmation_preserves_follower_and_no_business_secrets(self):
        original = state.node['node_id']
        response = await self.reset('0' * 32)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(state.node['node_id'], original)
        self.assertEqual(await media_crypto.business_role(), 'Follower')
        self.assertIsNone(media_crypto.manager)
        self.assertFalse((self.root / 'media-keys').exists())
        admin_nodes.runtime.stop.assert_not_awaited()

    async def test_follower_with_business_object_still_cannot_reset(self):
        async with self.database.begin() as conn:
            await media_objects.ensure_object(conn, 'music/artist/preserved.mp3', 'audio')
        response = await self.reset()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(await media_crypto.business_role(), 'Follower')
        self.assertIsNone(media_crypto.manager)
        admin_nodes.runtime.stop.assert_not_awaited()

    async def test_restarting_business_initialization_preserves_the_premaster(self):
        self.assertEqual((await self.reset()).status_code, 200)
        key_id = media_crypto.manager.key_id()
        await initialize_business_services()
        self.assertEqual(media_crypto.manager.key_id(), key_id)
        self.assertEqual((self.media / lyrics.DEFAULT_LYRIC_PATH).read_text(), lyrics.DEFAULT_LYRIC_CONTENT)
