"""Actual SQLite roster/nonce routes and signed client validation."""
import hashlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import insert, text, update

from app.api import internal_resource_registry as api
from app.services import media_manager
from app.services.federation import protocol as p, schema as fs
from app.services.resource_observation import ContentObserver
from app.services.resource_registry import BATCH, NativeRegistry, RegistryClient, RegistryError
from tests import test_resource_inventory_api as fixture


@unittest.skipUnless(os.name == 'posix','native roster uses Linux storage fixtures')
class RegistryTests(unittest.IsolatedAsyncioTestCase):
    verified = fixture.InventoryAPITests.verified

    async def asyncSetUp(self):
        await fixture.InventoryAPITests.asyncSetUp(self)
        self.observer = ContentObserver(self.store,traffic=self.inventory.traffic)
        self.registry = NativeRegistry(self.store,self.observer,self.inventory)
        item = patch.object(api,'registry',self.registry)
        item.start()
        self.patches.append(item)
        self.client._transport.app.include_router(api.router)
        self.master = SimpleNamespace(node=dict(node_id='c'*32,role='Master'),unseal=lambda value:value)
        self.downstream = dict(relationship_id=self.relationship,peer_id=self.store.node['node_id'],
            direction='downstream',state='active',peer_endpoint='https://storage.4399.com',
            peer_key=p.public_key(self.store.unseal(self.store.node['private_key'])),credential=self.credential)
        self.tamper = None
        self.wrong_key = False
        self.consumer = RegistryClient(self.master,self.downstream,SimpleNamespace(request=self.transport))

    async def asyncTearDown(self):
        await self.observer.stop()
        await fixture.InventoryAPITests.asyncTearDown(self)

    async def seed(self,count=1):
        async with self.database.begin() as conn:
            for number in range(count):
                path = f'music/album/{number}.mp3'
                await conn.execute(text('INSERT INTO media_objects VALUES (:id,:kind,:path,:locator,:created,:updated)'),
                    dict(id=f'{number+1:064x}',kind='audio',path=path,locator=hashlib.sha256(path.encode()).hexdigest(),
                         created='2026-01-01 00:00:00',updated='2026-01-01 00:00:00'))

    async def transport(self,origin,path,**kwargs):
        body = p.canonical(kwargs['value'])
        response = await self.client.post(path,content=body,
            headers=p.auth_headers(kwargs['credential'],kwargs['relation'],'POST',path,body))
        value = self.verified(response)
        if self.tamper is not None:
            value = self.tamper(value)
        return p.sign(p.new_key() if self.wrong_key else self.store.unseal(self.store.node['private_key']),value)

    async def test_complete_roster_is_bounded_and_never_opens_media_bytes(self):
        await self.seed(BATCH+5)
        received = []
        async def consume(value):
            received.append(value)
        with patch.object(media_manager,'resolve_safe_path',side_effect=AssertionError('roster read bytes')):
            result = await self.consumer.read(consume)
        self.assertEqual(result['count'],BATCH+5)
        self.assertEqual(len(received),BATCH+5)
        self.assertEqual(received[0]['path'],'music/album/0.mp3')
        self.assertEqual(result['incarnation'],self.observer.incarnation)
        self.assertEqual(result['inventory_incarnation'],self.inventory.incarnation)

    async def test_replayed_nonce_is_refused_and_unauthorized_calls_have_no_rows(self):
        path = '/internal/v1/migration/registry/media'
        body = p.canonical({'request_id':'d'*32,'after':''})
        headers = p.auth_headers(self.credential,self.relationship,'POST',path,body)
        self.assertEqual((await self.client.post(path,content=body,headers=headers)).status_code,200)
        self.assertEqual((await self.client.post(path,content=body,headers=headers)).status_code,401)
        self.assertEqual((await self.client.post(path,content=body)).status_code,401)

    async def test_invalid_types_and_page_binding_never_reach_consumer(self):
        await self.seed()
        for change in ({'version':True},{'node_id':'f'*32},{'complete':1},{'pending_operations':True},
                       {'pending_operations':1},{'next':'f'*64},{'extra':'value'}):
            self.tamper = lambda value:{**value,**change}
            async def consume(_):
                raise AssertionError('invalid roster consumed')
            with self.subTest(change=change), self.assertRaises(RegistryError):
                await self.consumer.read(consume)
        for change in ({'path':None},{'path':[]},{'path':'../escape'},{'object_kind':[]},
                       {'object_kind':'video'},{'object_id':'bad'},{'metadata_sha256':False}):
            self.tamper = lambda value:{**value,'entries':[{**value['entries'][0],**change}]}
            with self.subTest(entry=change), self.assertRaises(p.ProtocolError):
                await self.consumer.read(consume)

    async def test_restart_cursor_regression_and_forgery_fail_closed(self):
        await self.seed(BATCH+1)
        received = []
        async def consume(value):
            received.append(value)
        self.tamper = lambda value:{**value,'inventory_incarnation':'e'*32} if value['after'] else value
        with self.assertRaisesRegex(RegistryError,'incarnation_changed'):
            await self.consumer.read(consume)
        self.assertEqual(len(received),BATCH)
        self.tamper = None
        self.wrong_key = True
        with self.assertRaisesRegex(RegistryError,'signed_response'):
            await self.consumer.read(consume)
        self.wrong_key = False
        self.tamper = lambda value:{**value,'entries':[value['entries'][0],value['entries'][0]]}
        received.clear()
        with self.assertRaisesRegex(RegistryError,'entry_invalid'):
            await self.consumer.read(consume)
        self.assertEqual(received,[])

    async def test_revocation_and_unreconciled_operations_block_fresh_reads(self):
        await self.seed()
        async with self.database.begin() as conn:
            await conn.execute(insert(fs.upload_sessions).values(upload_id='f'*32,
                storage_member_id=self.store.node['node_id'],media_id='a'*64,media_path='music/new/song.mp3',
                path_locator='f'*64,object_kind='audio',expected_bytes=3,state='reserved',
                expires_at=1,created_at=1,updated_at=1))
        async def consume(_):
            raise AssertionError('pending-operation roster consumed')
        with self.assertRaisesRegex(RegistryError,'pending_or_invalid'):
            await self.consumer.read(consume)
        async with self.database.begin() as conn:
            await conn.execute(update(fs.relationships).values(state='revoked'))
        path = '/internal/v1/migration/registry/media'
        body = p.canonical({'request_id':'d'*32,'after':''})
        self.assertEqual((await self.client.post(path,content=body,
            headers=p.auth_headers(self.credential,self.relationship,'POST',path,body))).status_code,401)
