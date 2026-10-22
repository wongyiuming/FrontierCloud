"""Run complete unittest discovery in reproducible, independently checked shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ['git', *args], cwd=root, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
    )


def source_state(root: Path, allow_dirty: bool) -> dict:
    dirty = bool(git(root, 'status', '--porcelain', '-z', '--untracked-files=all'))
    if dirty and not allow_dirty:
        raise ValueError('Unit acceptance requires clean source; --allow-dirty is development-only')
    names = sorted(set(git(root, 'ls-files', '-co', '--exclude-standard', '-z').split(b'\0')) - {b''})
    digest = hashlib.sha256()
    for name in names:
        path = root / os.fsdecode(name)
        if path.is_symlink():
            content = b'link\0' + os.fsencode(os.readlink(path))
        elif path.is_file():
            content = b'file\0' + path.read_bytes()
        elif not path.exists():
            content = b'deleted'
        else:
            raise ValueError('Unexpected source entry: ' + os.fsdecode(name))
        digest.update(name + b'\0' + hashlib.sha256(content).digest())
    return {
        'head': git(root, 'rev-parse', 'HEAD').decode().strip(),
        'tree': git(root, 'rev-parse', 'HEAD^{tree}').decode().strip(),
        'content_sha256': digest.hexdigest(), 'dirty': dirty, 'entries': len(names),
    }


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def discover(root: Path) -> list:
    # Spawned test processes inherit sys.path and must also find the product.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    loader = unittest.TestLoader()
    tests = list(flatten(loader.discover(str(root / 'tests'), pattern='test_*.py')))
    if loader.errors:
        raise ValueError('Unit discovery failed:\n' + '\n'.join(loader.errors))
    if not tests:
        raise ValueError('Unit discovery found no tests')
    return tests


def shard_for(test, count: int) -> int:
    # Keep a module's setUpModule/setUpClass lifecycle on a single runner.
    module = type(test).__module__
    return int.from_bytes(hashlib.sha256(module.encode()).digest(), 'big') % count


def selected(tests: list, index: int, count: int) -> list:
    return [test for test in tests if shard_for(test, count) == index]


def inventory(tests: list) -> dict:
    ids = sorted(test.id() for test in tests)
    return {'count': len(ids), 'ids_sha256': hashlib.sha256(
        json.dumps(ids, ensure_ascii=True, separators=(',', ':')).encode(),
    ).hexdigest()}


def run_shard(tests: list, index: int, count: int, *, stream=None) -> dict:
    discovered = inventory(tests)
    cases = selected(tests, index, count)
    selected_ids = [test.id() for test in cases]
    suite = unittest.TestSuite(cases)
    # Do not keep completed fixture instances alive for the entire shard.
    # TestSuite normally releases each case; duplicate lists would defeat it.
    tests.clear()
    cases.clear()
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {
        'version': 1, 'shard_index': index, 'shard_count': count,
        'discovery': discovered, 'selected_ids': selected_ids,
        'tests_run': result.testsRun, 'skipped': [str(reason) for _, reason in result.skipped],
        'failures': len(result.failures), 'errors': len(result.errors),
        'duration_seconds': round(time.monotonic() - started, 6),
        'completed': not result.shouldStop, 'success': result.wasSuccessful() and not result.shouldStop,
    }


def verify(tests: list, source: dict, count: int, receipts: list[dict]) -> dict:
    if len(receipts) != count:
        raise ValueError('Missing or extra shard receipts')
    expected_inventory = inventory(tests)
    seen = set()
    all_ids = []
    for receipt in receipts:
        index = receipt.get('shard_index')
        if type(index) is not int or not 0 <= index < count or index in seen:
            raise ValueError('Invalid or duplicate shard index')
        seen.add(index)
        if (receipt.get('version') != 1 or receipt.get('shard_count') != count
                or receipt.get('source') != source or receipt.get('discovery') != expected_inventory):
            raise ValueError('Shard source or discovery does not match current source')
        expected = [test.id() for test in selected(tests, index, count)]
        if receipt.get('selected_ids') != expected:
            raise ValueError('Shard omitted, repeated or reordered a discovered test')
        if (receipt.get('success') is not True or receipt.get('completed') is not True
                or receipt.get('failures') != 0 or receipt.get('errors') != 0
                or type(receipt.get('tests_run')) is not int
                or not 0 <= receipt['tests_run'] <= len(expected)):
            raise ValueError('Shard did not complete successfully')
        all_ids.extend(expected)
    if sorted(all_ids) != sorted(test.id() for test in tests):
        raise ValueError('Shard coverage differs from complete discovery')
    return {'version': 1, 'source': source, 'discovery': expected_inventory,
            'shards': count, 'success': True,
            'tests_run': sum(item['tests_run'] for item in receipts),
            'skipped': sum(len(item['skipped']) for item in receipts)}


def external_receipt(path: str, root: Path) -> Path:
    target = Path(path).resolve()
    if target.is_relative_to(root):
        raise ValueError('Store unit receipts outside the source checkout')
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'verify'])
    parser.add_argument('--count', type=int, default=4)
    parser.add_argument('--index', type=int)
    parser.add_argument('--receipt')
    parser.add_argument('--receipts', nargs='+')
    parser.add_argument('--allow-dirty', action='store_true', help='Development overlay only; never release evidence')
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 32:
        parser.error('--count must be between 1 and 32')
    if args.action == 'run' and (args.index is None or not 0 <= args.index < args.count or not args.receipt):
        parser.error('run requires --index within shard count and --receipt')
    if args.action == 'verify' and (not args.receipts or args.index is not None):
        parser.error('verify requires --receipts and no --index')
    try:
        receipt_path = external_receipt(args.receipt, ROOT) if args.receipt else None
        if receipt_path and receipt_path.exists():
            raise ValueError('Receipt already exists; preserve the previous evidence')
        source = source_state(ROOT, args.allow_dirty)
        tests = discover(ROOT)
        if args.action == 'run':
            value = run_shard(tests, args.index, args.count)
            value['source'] = source
        else:
            receipts = [json.loads(external_receipt(name, ROOT).read_text()) for name in args.receipts]
            value = verify(tests, source, args.count, receipts)
        if source_state(ROOT, args.allow_dirty) != source:
            raise ValueError('Source changed during unit acceptance')
        if receipt_path:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            with receipt_path.open('x', encoding='utf-8') as output:
                json.dump(value, output, ensure_ascii=True, indent=2)
                output.write('\n')
        print(json.dumps({key: item for key, item in value.items() if key != 'selected_ids'}))
        return 0 if value['success'] else 1
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
