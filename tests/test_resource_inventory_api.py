"""Actual SQLite nonce/revocation and signed read-only inventory HTTP routes."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import insert, update
from sqlalchemy.engine import URL

from app.api import internal_nodes, internal_resource_inventory as api
from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.services.business_traffic import Monitor
from app.services.federation import protocol as p, schema as s
from app.services.federation.state import State
from app.services.resource_inventory import Inventory


@unittest.skipUnless(os.name == 'posix', 'native inventory uses product Linux openat')
class InventoryAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = create_sqlite_engine(URL.create('sqlite+aiosqlite', database=str(self.root/'state.db')))
        with patch.object(db, 'engine', self.database), patch.object(db, 'SECRET_DIR', self.root/'secrets'):
            await db.init_db()
        self.store = State(self.database, Fernet.generate_key())
        await self.store.initialize()
        await self.store.promote('Follower', 'https://storage.4399.com', 'test')
        self.relationship, self.credential = 'b'*32, p.encode(bytes(32))
        async with self.database.begin() as conn:
            await conn.execute(insert(s.relationships).values(relationship_id=self.relationship, peer_id='c'*32,
                peer_endpoint='https://master.4399.com', peer_key=p.public_key(p.new_key()),
                credential=self.store.seal(self.credential), direction='upstream', mode='Direct',
                state='active', status='online', last_heartbeat=1, rtt_ms=1, failures=0, recoveries=0,
                peer_version=p.APP_VERSION, protocol=p.PROTOCOL_VERSION, summary={}, created_at=1))
        self.files = self.root/'files'
        self.files.mkdir()
        (self.files/'one').write_bytes(b'original')
        traffic = Monitor(monotonic=lambda: 1000, wall=lambda: 1000)
        traffic._quiet_since = 900
        self.inventory = Inventory(root_provider=lambda: {'data': self.files}, traffic=traffic)
        self.patches = [patch.object(internal_nodes, 'state', self.store), patch.object(api, 'state', self.store),
                        patch.object(api, 'inventory', self.inventory), patch.object(internal_nodes.settings, 'TLS_ENABLED', True)]
        for item in self.patches:
            item.start()
        app = FastAPI()
        app.include_router(api.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://storage.4399.com')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.inventory.stop()
        for item in reversed(self.patches):
            item.stop()
        await self.database.dispose()
        self.temp.cleanup()

    async def request(self, action, value, *, raw=None):
        path = '/internal/v1/migration/inventory/' + action
        body = p.canonical(value) if raw is None else raw
        headers = p.auth_headers(self.credential, self.relationship, 'POST', path, body)
        return await self.client.post(path, content=body, headers=headers)

    def verified(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return p.verify(p.public_key(self.store.unseal(self.store.node['private_key'])), response.json())

    async def test_signed_session_page_and_close_with_no_file_mutation(self):
        header = self.verified(await self.request('start', {'request_id': 'd'*32, 'scope': 'data'}))
        self.assertEqual(header['node_id'], self.store.node['node_id'])
        page = self.verified(await self.request('page', {'session_id': header['session_id'], 'sequence': 0}))
        self.assertTrue(page['complete'])
        self.assertEqual(page['entries'][0]['path'], 'one')
        self.assertEqual(page['bytes'], 8)
        closed = self.verified(await self.request('close', {'session_id': header['session_id']}))
        self.assertTrue(closed['closed'])
        self.assertEqual((self.files/'one').read_bytes(), b'original')

    async def test_replayed_authentication_and_unsigned_requests_are_rejected(self):
        path = '/internal/v1/migration/inventory/start'
        body = p.canonical({'request_id': 'd'*32, 'scope': 'data'})
        headers = p.auth_headers(self.credential, self.relationship, 'POST', path, body)
        self.assertEqual((await self.client.post(path, content=body, headers=headers)).status_code, 200)
        self.assertEqual((await self.client.post(path, content=body, headers=headers)).status_code, 401)
        self.assertEqual((await self.client.post(path, content=body)).status_code, 401)

    async def test_revocation_blocks_existing_session_and_foreign_direction_is_refused(self):
        header = self.verified(await self.request('start', {'request_id': 'd'*32, 'scope': 'data'}))
        async with self.database.begin() as conn:
            await conn.execute(update(s.relationships).values(state='revoked'))
        response = await self.request('page', {'session_id': header['session_id'], 'sequence': 0})
        self.assertEqual(response.status_code, 401)
        async with self.database.begin() as conn:
            await conn.execute(update(s.relationships).values(state='active', direction='downstream'))
        self.assertEqual((await self.request('start', {'request_id': 'd'*32, 'scope': 'data'})).status_code, 409)

    async def test_caller_cannot_choose_roots_paths_or_bypass_https(self):
        for value in ({'request_id': 'd'*32, 'scope': '/etc'},
                      {'request_id': 'd'*32, 'scope': 'data', 'path': '/etc'},
                      {'request_id': 'd'*32, 'scope': []}):
            self.assertEqual((await self.request('start', value)).status_code, 409)
        self.assertEqual((await self.request('start', {}, raw=b'['*1500+b']'*1500)).status_code, 409)
        self.client.base_url = 'http://storage.4399.com'
        self.assertEqual((await self.request('start', {'request_id': 'd'*32, 'scope': 'data'})).status_code, 403)
        self.assertIsNone(self.inventory.session)
