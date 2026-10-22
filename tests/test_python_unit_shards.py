"""Protect full-discovery coverage and unittest fixture semantics in CI."""

import copy
import gc
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import weakref
import uuid

from scripts import test_python_units as units


class PythonUnitShardTests(unittest.TestCase):
    def cases(self):
        class First(unittest.TestCase):
            def test_one(self):
                pass

            def test_two(self):
                pass

        class Second(unittest.TestCase):
            def test_three(self):
                pass

        First.__module__ = 'test_first'
        First.__qualname__ = 'First'
        Second.__module__ = 'test_second'
        Second.__qualname__ = 'Second'
        loader = unittest.TestLoader()
        return list(units.flatten(unittest.TestSuite([
            loader.loadTestsFromTestCase(First), loader.loadTestsFromTestCase(Second),
            First('test_one'),
        ])))

    def receipts(self, tests, count=4):
        source = {'head': 'a' * 40, 'content_sha256': 'b' * 64, 'dirty': False}
        values = []
        for index in range(count):
            value = units.run_shard(list(tests), index, count, stream=io.StringIO())
            value['source'] = source
            values.append(value)
        return source, values

    def test_complete_inventory_preserves_duplicate_ids_and_class_fixtures(self):
        events = []

        class Fixture(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                events.append('setup')

            @classmethod
            def tearDownClass(cls):
                events.append('teardown')

            def test_one(self):
                events.append('one')

            def test_two(self):
                events.append('two')

        Fixture.__module__ = 'test_fixture'
        Fixture.__qualname__ = 'Fixture'
        tests = [Fixture('test_one'), Fixture('test_two'), Fixture('test_one')]
        source, receipts = self.receipts(tests)
        proof = units.verify(tests, source, 4, receipts)
        self.assertEqual(proof['discovery']['count'], 3)
        self.assertEqual(proof['tests_run'], 3)
        self.assertEqual(events, ['setup', 'one', 'two', 'one', 'teardown'])

    def test_aggregate_rejects_missing_extra_stale_or_tampered_shards(self):
        tests = self.cases()
        source, receipts = self.receipts(tests)
        self.assertTrue(units.verify(tests, source, 4, receipts)['success'])
        bad = [receipts[:-1], receipts + [receipts[0]], [receipts[0]] * 4]
        occupied = next(i for i, item in enumerate(receipts) if item['selected_ids'])
        for key, replacement in [('source', {}), ('discovery', {}), ('success', False),
                                 ('completed', False), ('failures', 1), ('errors', 1),
                                 ('shard_count', 3), ('tests_run', True),
                                 ('selected_ids', receipts[occupied]['selected_ids'][:-1])]:
            changed = copy.deepcopy(receipts)
            changed[occupied][key] = replacement
            bad.append(changed)
        for candidate in bad:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                units.verify(tests, source, 4, candidate)
        with self.assertRaises(ValueError):
            units.verify(tests[:-1], source, 4, receipts)

    def test_class_skip_is_complete_without_inventing_executed_cases(self):
        class Skipped(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest('dedicated native fixture')

            def test_one(self):
                self.fail('class should skip')

        tests = [Skipped('test_one')]
        source, receipts = self.receipts(tests)
        proof = units.verify(tests, source, 4, receipts)
        self.assertEqual(proof['tests_run'], 0)
        self.assertEqual(proof['skipped'], 1)

    def test_failing_case_cannot_satisfy_aggregate(self):
        class Failed(unittest.TestCase):
            def test_failure(self):
                self.fail('intentional runner fixture failure')

        tests = [Failed('test_failure')]
        source, receipts = self.receipts(tests)
        with self.assertRaisesRegex(ValueError, 'successfully'):
            units.verify(tests, source, 4, receipts)

    def test_module_assignment_does_not_depend_on_custom_ids(self):
        class Custom(unittest.TestCase):
            def runTest(self):
                pass

            def id(self):
                return 'custom-id-without-module'

        class Other(unittest.TestCase):
            def runTest(self):
                pass

        Custom.__module__ = Other.__module__ = 'test_shared_module'
        for count in (1, 4, 32):
            self.assertEqual(units.shard_for(Custom(), count), units.shard_for(Other(), count))

    def test_source_content_includes_untracked_deleted_and_changed_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'tracked.txt').write_text('original')
            subprocess.run(['git', 'add', 'tracked.txt'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.test',
                            'commit', '-qm', 'fixture'], cwd=root, check=True)
            original = units.source_state(root, False)
            (root / 'tracked.txt').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'clean source'):
                units.source_state(root, False)
            changed = units.source_state(root, True)
            (root / 'new.txt').write_text('additional input')
            added = units.source_state(root, True)
            (root / 'tracked.txt').unlink()
            deleted = units.source_state(root, True)
            self.assertEqual(len({item['content_sha256'] for item in (original, changed, added, deleted)}), 4)
            self.assertTrue(deleted['dirty'])

    def test_receipts_cannot_change_source_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaisesRegex(ValueError, 'outside'):
                units.external_receipt(str(root / 'receipt.json'), root)
            target = units.external_receipt(str(root.parent / 'receipt.json'), root)
            self.assertFalse(target.is_relative_to(root))

    def test_completed_fixture_is_released_before_the_next_case_runs(self):
        owner=self
        class First(unittest.TestCase):
            def runTest(self):
                self.fixture_payload=bytearray(5*1024*1024)
        class Second(unittest.TestCase):
            def runTest(self):
                gc.collect()
                owner.assertIsNone(reference(),'Completed fixture is still retained by discovery/shard lists')
        first=First();reference=weakref.ref(first)
        tests=[first,Second()];del first
        result=units.run_shard(tests,0,1,stream=io.StringIO())
        self.assertTrue(result['success']);self.assertEqual(result['tests_run'],2)
        self.assertEqual(len(result['selected_ids']),2);self.assertEqual(tests,[])

    def test_spawned_test_child_can_import_source_after_discovery(self):
        token=uuid.uuid4().hex;module='test_spawn_'+token;payload='worker_payload_'+token
        before=list(sys.path)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tests').mkdir()
            (root/(payload+'.py')).write_text("VALUE='source-visible'\n")
            source="""import multiprocessing,unittest
def worker(sender):
    from PAYLOAD import VALUE
    sender.send(VALUE)
    sender.close()
class SpawnCase(unittest.TestCase):
    def test_child_source_import(self):
        context=multiprocessing.get_context('spawn')
        receiver,sender=context.Pipe(duplex=False)
        process=context.Process(target=worker,args=(sender,))
        process.start();sender.close()
        try:
            self.assertTrue(receiver.poll(10),'Spawned child could not import source')
            self.assertEqual(receiver.recv(),'source-visible')
            process.join(10);self.assertEqual(process.exitcode,0)
        finally:
            if process.is_alive():process.kill();process.join(10)
            receiver.close()
""".replace('PAYLOAD',payload)
            (root/'tests'/(module+'.py')).write_text(source)
            try:
                tests=units.discover(root)
                result=units.run_shard(tests,0,1,stream=io.StringIO())
                self.assertTrue(result['success']);self.assertEqual(result['tests_run'],1)
            finally:
                sys.path[:]=before
                for name in (module,payload):sys.modules.pop(name,None)


if __name__ == '__main__':
    unittest.main()
