"""Actual native filesystem and authenticated ASGI inventory regressions."""
import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.core.joined_io import joined_io
from app.services.business_traffic import Monitor
from app.services.federation import protocol as p
from app.services.resource_inventory import Inventory, InventoryError, PAGE_BYTES, PAGE_COUNT, SESSION_SECONDS, Walker

NODE = dict(node_id='a'*32, role='Follower')
RELATION = dict(relationship_id='b'*32, peer_id='c'*32, direction='upstream', state='active')


class JoinedIOTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_and_helper_cancellation_join_actual_work(self):
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            if not release.wait(3):
                raise AssertionError('test worker was not released')
            return 1
        task = asyncio.create_task(joined_io(blocked))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            for worker in asyncio.all_tasks():
                if worker.get_name() == 'joined-file-io':
                    worker.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)

    async def test_cancelled_resource_creation_closes_its_unreturned_result(self):
        entered, release = threading.Event(), threading.Event()
        disposed = []
        def create():
            entered.set()
            if not release.wait(3):
                raise AssertionError('test worker was not released')
            return 'resource'
        task = asyncio.create_task(joined_io(create, on_cancel=disposed.append))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(disposed, ['resource'])

    async def test_error_propagates_and_queued_cancel_does_not_start_operation(self):
        def error():
            raise OSError('read failed')
        with self.assertRaisesRegex(OSError, 'read failed'):
            await joined_io(error)
        calls = []
        async def queued(_work):
            await asyncio.Event().wait()
        with patch('app.core.joined_io.asyncio.to_thread', queued):
            task = asyncio.create_task(joined_io(calls.append, 1))
            await asyncio.sleep(0)
            worker = next(worker for worker in asyncio.all_tasks() if worker.get_name() == 'joined-file-io')
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(calls, [])


@unittest.skipUnless(os.name == 'posix', 'native inventory uses product Linux openat')
class NativeInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root/'data'
        self.data.mkdir()
        self.now = 1000.0
        self.traffic = Monitor(monotonic=lambda: self.now, wall=lambda: self.now)
        self.now += 30
        self.inventory = Inventory(root_provider=lambda: dict(data=self.data, media=self.data/'media',
            recordings=self.data/'recordings'), traffic=self.traffic,
            clock=lambda: self.now, wall=lambda: self.now)

    async def asyncTearDown(self):
        await self.inventory.stop()
        self.temp.cleanup()

    async def start(self, scope='data', request='d'*32):
        return await self.inventory.start(NODE, RELATION, request, scope)

    async def test_many_files_have_bounded_pages_exact_transcript_and_no_reads(self):
        parent = self.data/'media/music/历史'
        parent.mkdir(parents=True)
        for index in range(1074):
            (parent/f'{index:04d}.mp3').write_bytes(b'x')
        header = await self.start()
        self.assertFalse(header['writable_verified'])
        expected = hashlib.sha256(p.canonical(header))
        sequence, count, size = 0, 0, 0
        with patch('builtins.open', side_effect=AssertionError('file contents read')):
            while True:
                page = await self.inventory.page(NODE, RELATION, header['session_id'], sequence)
                self.assertLessEqual(len(p.canonical(page)), PAGE_BYTES)
                self.assertLessEqual(len(page['entries']), PAGE_COUNT)
                for entry in page['entries']:
                    raw = p.canonical(entry)
                    expected.update(len(raw).to_bytes(4, 'big'))
                    expected.update(raw)
                    count += 1
                    size += entry['bytes']
                self.assertEqual((page['count'], page['bytes']), (count, size))
                self.assertEqual(page['transcript_sha256'], expected.hexdigest())
                duplicate = await self.inventory.page(NODE, RELATION, header['session_id'], sequence)
                self.assertEqual(page, duplicate)
                sequence += 1
                if page['complete']:
                    break
        self.assertEqual(size, 1074)
        self.assertEqual(count, 1077)
        self.assertGreater(sequence, 16)
        self.assertEqual(self.inventory.session.walker.frames, [])

    async def test_long_names_respect_payload_budget(self):
        directory = self.data
        for _ in range(3):
            directory = directory/('长'*70)
            directory.mkdir()
        for index in range(70):
            (directory/(str(index)+'曲'*65)).write_bytes(b'')
        header = await self.start()
        sequence, maximum = 0, 0
        while True:
            page = await self.inventory.page(NODE, RELATION, header['session_id'], sequence)
            maximum = max(maximum, len(p.canonical(page)))
            self.assertLessEqual(len(p.canonical(page)), PAGE_BYTES)
            sequence += 1
            if page['complete']:
                break
        self.assertGreater(maximum, 30000)

    async def test_missing_root_is_observed_without_creating_it(self):
        header = await self.start('media')
        self.assertFalse(header['root_exists'])
        page = await self.inventory.page(NODE, RELATION, header['session_id'], 0)
        self.assertEqual(page['entries'], [])
        self.assertTrue(page['complete'])
        self.assertFalse((self.data/'media').exists())

    async def test_symlink_and_fifo_are_reported_without_following_or_opening(self):
        outside = self.root/'outside'
        outside.mkdir()
        (outside/'secret').write_bytes(b'private')
        (self.data/'link').symlink_to(outside, target_is_directory=True)
        os.mkfifo(self.data/'fifo')
        header = await self.start()
        page = await self.inventory.page(NODE, RELATION, header['session_id'], 0)
        self.assertEqual({entry['kind'] for entry in page['entries']}, {'symlink', 'other'})
        self.assertNotIn('secret', str(page))
        self.assertEqual((outside/'secret').read_bytes(), b'private')

    async def test_root_symlink_is_refused(self):
        (self.data/'media').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            await self.start('media')
        self.assertIsNone(self.inventory.session)

    async def test_hard_link_identity_is_not_hidden(self):
        (self.data/'one').write_bytes(b'abc')
        os.link(self.data/'one', self.data/'two')
        header = await self.start()
        page = await self.inventory.page(NODE, RELATION, header['session_id'], 0)
        self.assertEqual([entry['links'] for entry in page['entries']], [2, 2])
        self.assertEqual(page['entries'][0]['inode'], page['entries'][1]['inode'])

    async def test_directory_mutation_cannot_yield_a_complete_proof(self):
        header = await self.start()
        (self.data/'changed').write_bytes(b'x')
        with self.assertRaisesRegex(InventoryError, 'changed'):
            await self.inventory.page(NODE, RELATION, header['session_id'], 0)
        self.assertIsNone(self.inventory.session)
        self.assertTrue((self.data/'changed').exists())

    async def test_missing_root_appearing_invalidates_observation(self):
        header = await self.start('media')
        (self.data/'media').mkdir()
        with self.assertRaisesRegex(InventoryError, 'changed'):
            await self.inventory.page(NODE, RELATION, header['session_id'], 0)

    async def test_fixed_deadline_busy_requests_and_wrong_owner(self):
        header = await self.start()
        self.now += 5
        self.assertEqual(header, await self.start())
        with self.assertRaisesRegex(InventoryError, 'busy'):
            await self.start(request='e'*32)
        with self.assertRaises(InventoryError):
            await self.inventory.page(NODE, {**RELATION, 'peer_id': 'f'*32}, header['session_id'], 0)
        self.assertIsNotNone(self.inventory.session)
        walker = self.inventory.session.walker
        self.now += SESSION_SECONDS
        with self.assertRaisesRegex(InventoryError, 'unavailable'):
            await self.inventory.page(NODE, RELATION, header['session_id'], 0)
        self.assertEqual(walker.frames, [])

    async def test_high_traffic_waits_without_advancing_or_extending_session(self):
        header = await self.start()
        self.traffic.record(egress=262144)
        first = await self.inventory.page(NODE, RELATION, header['session_id'], 0)
        self.assertTrue(first['waiting_low_traffic'])
        self.assertEqual(self.inventory.session.sequence, 0)
        self.now += 5
        self.traffic.snapshot()
        self.now += 30
        self.assertTrue((await self.inventory.page(NODE, RELATION, header['session_id'], 0))['complete'])
        self.assertEqual(self.inventory.session.header['expires_at'], header['expires_at'])

    async def test_bad_sequence_closes_only_its_session_and_never_deletes_files(self):
        (self.data/'keep').write_bytes(b'keep')
        header = await self.start()
        with self.assertRaisesRegex(InventoryError, 'sequence'):
            await self.inventory.page(NODE, RELATION, header['session_id'], 7)
        self.assertIsNone(self.inventory.session)
        self.assertEqual((self.data/'keep').read_bytes(), b'keep')

    async def test_cancelled_page_joins_before_closing_native_descriptors(self):
        header = await self.start()
        walker = self.inventory.session.walker
        entered, release = threading.Event(), threading.Event()
        original = walker.page
        def blocked():
            entered.set()
            if not release.wait(3):
                raise AssertionError('test worker not released')
            return original()
        with patch.object(walker, 'page', blocked):
            task = asyncio.create_task(self.inventory.page(NODE, RELATION, header['session_id'], 0))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
                self.assertTrue(walker.frames)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
        self.assertEqual(walker.frames, [])
        self.assertIsNone(self.inventory.session)

    async def test_repeated_close_and_restart_cannot_reuse_sessions(self):
        header = await self.start()
        await self.inventory.close(NODE, RELATION, header['session_id'])
        await self.inventory.close(NODE, RELATION, header['session_id'])
        self.assertIsNone(self.inventory.session)
        restarted = Inventory(root_provider=lambda: {'data': self.data}, traffic=self.traffic)
        try:
            with self.assertRaises(InventoryError):
                await restarted.page(NODE, RELATION, header['session_id'], 0)
            self.assertNotEqual(restarted.incarnation, header['incarnation'])
        finally:
            await restarted.stop()

    async def test_reaper_shutdown_closes_abandoned_session(self):
        await self.start()
        walker = self.inventory.session.walker
        task = asyncio.create_task(self.inventory.reap())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(walker.frames, [])

    async def test_scanner_limits_depth_and_does_not_leak_descriptors(self):
        directory = self.data
        for _ in range(65):
            directory = directory/'d'
            directory.mkdir()
        count = len(list(Path('/proc/self/fd').iterdir()))
        header = await self.start()
        with self.assertRaisesRegex(InventoryError, 'depth'):
            for sequence in range(10):
                await self.inventory.page(NODE, RELATION, header['session_id'], sequence)
        self.assertIsNone(self.inventory.session)
        self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())), count)
