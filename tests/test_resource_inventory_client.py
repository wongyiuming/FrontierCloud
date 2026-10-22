"""Signed transcript binding, bounded consumption and adversarial peer replies."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import httpx

from app.services.business_traffic import Monitor
from app.services.federation import protocol as p
from app.services.resource_inventory import Inventory, InventoryError
from app.services.resource_inventory_client import InventoryClient


@unittest.skipUnless(os.name == 'posix', 'native inventory uses product Linux openat')
class InventoryClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000.0
        traffic = Monitor(monotonic=lambda: self.now, wall=lambda: self.now)
        self.now += 30
        self.server = Inventory(root_provider=lambda: {'data': self.root}, traffic=traffic,
                                clock=lambda: self.now, wall=lambda: self.now)
        self.private = p.new_key()
        self.peer = dict(node_id='a'*32, role='Follower')
        self.upstream = dict(relationship_id='b'*32, peer_id='c'*32, direction='upstream', state='active')
        self.master = SimpleNamespace(node=dict(node_id='c'*32, role='Master'), unseal=lambda value: value)
        self.relation = dict(relationship_id='b'*32, peer_id='a'*32, direction='downstream', state='active',
            peer_endpoint='https://storage.4399.com', peer_key=p.public_key(self.private), credential=p.encode(bytes(32)))
        self.calls = []
        self.tamper = None
        self.fail_close = False
        self.wrong_key = False
        self.client = InventoryClient(self.master, self.relation, SimpleNamespace(request=self.request),
                                      clock=lambda: self.now, wall=lambda: self.now)

    async def asyncTearDown(self):
        await self.server.stop()
        self.temp.cleanup()

    async def request(self, origin, path, **kwargs):
        self.assertEqual(origin, self.relation['peer_endpoint'])
        self.assertEqual(kwargs['method'], 'POST')
        self.assertEqual(kwargs['relation'], self.relation['relationship_id'])
        self.assertEqual(kwargs['credential'], self.relation['credential'])
        action = path.rsplit('/', 1)[1]
        self.calls.append(action)
        if action == 'close' and self.fail_close:
            raise httpx.ConnectError('unreachable fixture')
        result = await getattr(self.server, action)(self.peer, self.upstream, **kwargs['value'])
        if result is None:
            result = {'closed': True}
        if self.tamper is not None:
            result = self.tamper(action, dict(result))
        return p.sign(p.new_key() if self.wrong_key else self.private, result)

    async def test_all_pages_verify_before_bounded_consumer_receives_them(self):
        for index in range(1074):
            (self.root/str(index)).write_bytes(b'abc')
        received = set()
        async def consume(value):
            self.assertNotIn(value['path'], received)
            received.add(value['path'])
        proof = await self.client.read('data', consume)
        self.assertEqual(len(received), 1074)
        self.assertEqual((proof['count'], proof['bytes']), (1074, 1074*3))
        self.assertEqual(self.calls[0], 'start')
        self.assertEqual(self.calls[-1], 'close')
        self.assertIsNone(self.server.session)

    async def test_wrong_identity_scope_incarnation_and_expiry_are_rejected(self):
        for name, wrong in [('master_id', 'd'*32), ('node_id', 'd'*32), ('scope', 'media'),
                            ('request_id', 'd'*32), ('expires_at', 999999), ('version', True),
                            ('physical_free_bytes', -1), ('writable_verified', True)]:
            async def consume(_):
                raise AssertionError('unbound inventory consumed')
            self.tamper = lambda action, value: {**value, name: wrong} if action == 'start' else value
            with self.subTest(name=name), self.assertRaises(InventoryError):
                await self.client.read('data', consume)
            await self.server.stop()
        self.tamper = lambda action, value: {**value, 'incarnation': 'd'*32} if action == 'page' else value
        with self.assertRaisesRegex(InventoryError, 'binding'):
            await self.client.read('data', consume)

    async def test_forged_signature_never_reaches_consumer(self):
        self.wrong_key = True
        async def consume(_):
            raise AssertionError('unsigned inventory consumed')
        with self.assertRaisesRegex(InventoryError, 'signed_response'):
            await self.client.read('data', consume)

    async def test_changed_transcript_counts_or_sequence_never_reach_consumer(self):
        (self.root/'one').write_bytes(b'abc')
        for name, wrong in [('count', 99), ('bytes', 99), ('sequence', True),
                            ('transcript_sha256', 'f'*64)]:
            self.tamper = lambda action, value: {**value, name: wrong} if action == 'page' else value
            received = []
            async def consume(value):
                received.append(value)
            with self.subTest(name=name), self.assertRaises(InventoryError):
                await self.client.read('data', consume)
            self.assertEqual(received, [])
            self.assertIsNone(self.server.session)

    async def test_unexpected_paths_and_unknown_fields_are_refused(self):
        (self.root/'one').write_bytes(b'abc')
        for invalid in ({'path': '../escape'}, {'kind': 'device'}, {'bytes': -1}, {'extra': 'value'}):
            def tamper(action, value):
                if action == 'page':
                    value['entries'] = [{**value['entries'][0], **invalid}]
                return value
            self.tamper = tamper
            async def consume(_):
                raise AssertionError('invalid entry consumed')
            with self.subTest(invalid=invalid), self.assertRaises(InventoryError):
                await self.client.read('data', consume)

    async def test_close_failure_does_not_discard_verified_observation(self):
        self.fail_close = True
        async def consume(_):
            pass
        result = await self.client.read('data', consume)
        self.assertEqual(result['count'], 0)
        self.assertIsNotNone(self.server.session)
        self.now += 301
        async with self.server.lock:
            await self.server._expire()
        self.assertIsNone(self.server.session)

    async def test_consumer_failure_or_cancel_closes_session(self):
        (self.root/'one').write_bytes(b'abc')
        for error in (RuntimeError('observation store failed'), asyncio.CancelledError()):
            async def consume(_):
                raise error
            with self.assertRaises(type(error)):
                await self.client.read('data', consume)
            self.assertIsNone(self.server.session)
