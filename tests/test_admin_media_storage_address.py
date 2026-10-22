"""Admin-only placement addresses from real SQL, with no peer or provider I/O."""
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy import event, insert, update

from app.api.v1 import admin_cluster_integrity as admin
from app.api.v1.admin_transport import require_secure_admin_transport
from app.services import media_manager, s3_schema
from app.services.federation import schema as fs
from app.services.federation.catalog import Catalog
from app.services.s3_signing import Credentials, Location
from tests import test_s3_uploads as fixtures


class AdminMediaStorageAddressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixtures.S3UploadTests(methodName='runTest')
        await self.f.asyncSetUp()
        self.f.store.node.update(role='Master', endpoint='https://master.example.test')
        self.catalog = Catalog(self.f.store)
        self.patches = [patch.object(admin, 'node_state', self.f.store),
                        patch.object(admin, 'node_catalog', self.catalog)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.f.asyncTearDown()

    async def native(self, owner, endpoint=None, *, kind='Follower', transport='Direct',
                     offline=False, peer=None, direction='downstream'):
        relationship = hashlib.sha256(owner.encode()).hexdigest()[:32] if endpoint else None
        async with self.f.database.begin() as conn:
            if relationship:
                await conn.execute(insert(fs.relationships).values(relationship_id=relationship,
                    peer_id=peer or owner, peer_endpoint=endpoint, peer_key='f' * 64,
                    credential='private-relationship-value', direction=direction, mode=transport,
                    state='active', status='offline' if offline else 'online', last_heartbeat=1,
                    rtt_ms=1, failures=0, recoveries=0, peer_version='fixture', protocol=1,
                    summary={}, created_at=1))
            await conn.execute(insert(fs.storage_members).values(member_id=owner,
                relationship_id=relationship, member_kind=kind, transport=transport,
                storage_enabled=1, allocated_bytes=1000, used_bytes=0, reserved_bytes=0,
                physical_free_bytes=1000, health='offline' if offline else 'online',
                writable=int(not offline), updated_at=1))

    async def media(self, owner, path):
        identifier = hashlib.sha256(path.encode()).hexdigest()
        async with self.f.database.begin() as conn:
            await conn.execute(insert(fs.global_media).values(media_id=identifier,
                storage_member_id=owner, object_id=identifier, media_path=path,
                path_locator=identifier, object_kind='audio', size_bytes=14,
                etag='fixture', state='active', created_at=1, updated_at=1))
        return identifier

    @staticmethod
    def file(owner, number=1):
        return dict(kind='file', media_id=f'{number:064x}', storage_member_id=owner)

    async def test_tree_and_search_keep_each_actual_owner_and_offline_endpoint(self):
        local = self.f.store.node['node_id']
        await self.native(local, kind='MasterLocal', transport='Local')
        await self.native('1' * 32, 'https://direct.example.test')
        await self.native('2' * 32, 'https://relay.example.test:8443', transport='Relay', offline=True)
        expected = {'local.mp3': ('https://master.example.test', True),
                    'direct.mp3': ('https://direct.example.test', False),
                    'relay.mp3': ('https://relay.example.test:8443', False)}
        for owner, name in ((local, 'local.mp3'), ('1' * 32, 'direct.mp3'), ('2' * 32, 'relay.mp3')):
            await self.media(owner, 'music/artist/album/' + name)
        tree = await admin.admin_tree(None, 'music/artist/album', 'actor')
        search = await admin.admin_tree_search(None, 'mp3', 'music/artist', 'actor')
        for result in (tree, search):
            self.assertEqual({item['name']: (item['storage_address'], item['storage_address_local'])
                              for item in result['items']}, expected)
        directories = await admin.admin_tree(None, 'music/artist', 'actor')
        self.assertEqual([item['kind'] for item in directories['items']], ['directory'])
        self.assertNotIn('storage_address', directories['items'][0])
        self.assertNotIn('storage_address_local', directories['items'][0])
        self.assertNotIn('storage_address', (await self.catalog.resources(directory='music/artist'))[0])

    async def test_unknown_mismatched_peer_and_wrong_local_identity_never_use_master(self):
        await self.native('1' * 32, 'https://wrong-peer.example.test', peer='9' * 32)
        await self.native('2' * 32)
        await self.native('3' * 32, kind='MasterLocal', transport='Local')
        await self.native('4' * 32, 'https://upstream.example.test', direction='upstream')
        await self.native('6' * 32, 'https://user:private@wrong.example.test')
        await self.native('7' * 32, 'https://wrong.example.test/?token=private')
        items = [self.file(str(number) * 32, number) for number in range(1, 8)]
        result = await admin._tree_storage_modes(items)
        self.assertTrue(all(item['storage_address'] is None for item in result))
        self.assertTrue(all(item['storage_address_local'] is False for item in result))

    async def test_published_s3_tree_and_search_add_only_unsigned_bucket_address(self):
        upload = await self.f.prepared(path='music/album/song.mp3')
        await self.f.service.complete(upload, actor='admin')
        with patch.object(self.f.store, 'unseal', side_effect=AssertionError('must not unseal credentials')):
            tree = await admin.admin_tree(None, 'music/album', 'actor')
            search = await admin.admin_tree_search(None, 'song', 'music', 'actor')
        for item in tree['items'] + search['items']:
            self.assertEqual(item['storage_address'], 'https://private-bucket.s3.us-east-1.amazonaws.com/')
            self.assertFalse(item['storage_address_local'])
            self.assertEqual(item['transport'], 'S3')
            self.assertFalse(urlsplit(item['storage_address']).query)
            self.assertNotIn('private-secret', json.dumps(item))
            self.assertNotIn('X-Amz-', json.dumps(item))

    async def test_s3_path_virtual_and_regional_addresses_use_no_credentials_or_object_key(self):
        locations = [
            (Location('path-bucket', 'us-east-1', 'https://objects.example.test:9443', 'path', 'private-prefix'),
             'https://objects.example.test:9443/path-bucket/'),
            (Location('virtual-bucket', 'us-east-1', 'https://objects.example.test', 'virtual', 'private-prefix'),
             'https://virtual-bucket.objects.example.test/'),
            (Location('regional-bucket', 'cn-north-1'), 'https://regional-bucket.s3.cn-north-1.amazonaws.com.cn/'),
        ]
        items, expected = [], {}
        for number, (location, address) in enumerate(locations, 1):
            owner = await self.f.members.add('address', location,
                Credentials('FixtureAccess123', 'private-credential-must-not-be-read'), 1000, 'actor')
            items.append(self.file(owner, number))
            expected[owner] = address
        statements = []
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(self.f.database.sync_engine, 'before_cursor_execute', observe)
        try:
            with patch.object(self.f.store, 'unseal', side_effect=AssertionError('must not unseal credentials')):
                await admin._tree_storage_modes(items)
        finally:
            event.remove(self.f.database.sync_engine, 'before_cursor_execute', observe)
        for item in items:
            self.assertEqual(item['storage_address'], expected[item['storage_member_id']])
            self.assertFalse(item['storage_address_local'])
            self.assertNotIn('private-prefix', item['storage_address'])
            self.assertFalse(urlsplit(item['storage_address']).query)
        self.assertTrue(any('s3_storage_members' in sql for sql in statements))
        self.assertTrue(all('credentials_sealed' not in sql for sql in statements))

    async def test_foreign_or_corrupt_s3_location_never_leaks_query_values(self):
        for values in ({'master_id': 'f' * 32}, {'location_json': json.dumps(dict(
                bucket='private-bucket', region='us-east-1', endpoint='https://objects.example.test/?token=private'))}):
            async with self.f.database.begin() as conn:
                await conn.execute(update(s3_schema.members).where(
                    s3_schema.members.c.member_id == self.f.member).values(
                        **{'master_id': self.f.store.node['node_id'], **values}))
            item = (await admin._tree_storage_modes([self.file(self.f.member)]))[0]
            self.assertIsNone(item['storage_address'])
            self.assertFalse(item['storage_address_local'])
            self.assertNotIn('private', json.dumps(item))

    async def test_address_queries_are_deduplicated_and_chunked_in_one_connection(self):
        for distinct, expected_queries in ((False, 2), (True, 4)):
            statements = []
            def observe(conn, _cursor, statement, parameters, _context, _many):
                if 'FROM cluster_storage_members' in statement or 'FROM s3_storage_members' in statement:
                    statements.append((id(conn), statement, len(parameters)))
            items = [self.file(f'{number if distinct else 1:032x}', number) for number in range(1, 502)]
            event.listen(self.f.database.sync_engine, 'before_cursor_execute', observe)
            try:
                await admin._tree_storage_modes(items)
            finally:
                event.remove(self.f.database.sync_engine, 'before_cursor_execute', observe)
            self.assertEqual(len(statements), expected_queries)
            self.assertEqual(len({connection for connection, _sql, _count in statements}), 1)
            self.assertTrue(all(count <= 501 for _connection, _sql, count in statements))
            self.assertTrue(all(item['storage_address'] is None for item in items))

    async def test_empty_and_directory_only_results_do_not_open_database(self):
        connect = Mock(side_effect=AssertionError('directory labels need no owner query'))
        with patch.object(admin, 'node_state', SimpleNamespace(database=SimpleNamespace(connect=connect))):
            for items in ([], [dict(kind='directory', path='music/album')]):
                self.assertIs(await admin._tree_storage_modes(items), items)
        connect.assert_not_called()

    async def test_legacy_filesystem_tree_and_search_explicitly_allow_browser_origin(self):
        root = self.f.root / 'media'
        (root / 'music/album/empty').mkdir(parents=True)
        (root / 'music/album/song.mp3').write_bytes(b'local media')
        (root / 'lyrics').mkdir()
        (root / 'lyrics/song.lrc').write_text('[00:00]local')
        with patch.object(media_manager, 'MEDIA_ROOT', root), \
                patch.object(media_manager.MediaManager, 'hidden_paths', new=AsyncMock(return_value=set())), \
                patch('app.services.federation.state.state', self.f.store):
            for role, endpoint, path in (('Standalone', '', 'music/album'),
                                         ('Master', 'https://master.example.test', 'lyrics')):
                self.f.store.node.update(role=role, endpoint=endpoint)
                results = [await admin.admin_tree(None, path, 'actor'),
                           await admin.admin_tree_search(None, 'song', path, 'actor')]
                for result in results:
                    for item in result['items']:
                        if item['kind'] == 'file':
                            self.assertEqual(item['storage_address'], endpoint or None)
                            self.assertTrue(item['storage_address_local'])
                        else:
                            self.assertNotIn('storage_address', item)
                            self.assertNotIn('storage_address_local', item)

    async def test_http_and_unauthenticated_requests_cannot_read_addresses(self):
        await self.native('1' * 32, 'https://direct.example.test')
        await self.media('1' * 32, 'music/album/song.mp3')
        async def session(request: Request):
            if request.cookies.get('admin') != 'fixture':
                raise HTTPException(401, 'sign in')
            return 'actor'
        app = FastAPI()
        app.dependency_overrides[admin.require_session] = session
        app.include_router(admin.router, prefix='/api/v1/media/admin',
                           dependencies=[Depends(require_secure_admin_transport)])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('192.0.2.1', 1234)),
                                     base_url='https://admin.example.test') as client:
            for suffix in ('/tree?path=music/album', '/tree/search?q=song&path=music/album'):
                url = '/api/v1/media/admin' + suffix
                denied = await client.get(url)
                self.assertEqual(denied.status_code, 401)
                self.assertNotIn('direct.example.test', denied.text)
                client.cookies.set('admin', 'fixture')
                self.assertEqual((await client.get('http://admin.example.test' + url)).status_code, 426)
                response = await client.get(url)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['items'][0]['storage_address'], 'https://direct.example.test')
                client.cookies.clear()


if __name__ == '__main__':
    unittest.main()
