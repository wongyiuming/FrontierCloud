"""Verify signed native inventory transcripts without buffering the whole tree."""
import asyncio
import hashlib
import time
import uuid

import httpx

from app.services.federation import protocol as p
from app.services.resource_inventory import PAGE_BYTES, PAGE_COUNT, SESSION_SECONDS, InventoryError
from app.services.resource_control_client import signed_request

ENTRY_FIELDS = frozenset({'path', 'kind', 'bytes', 'device', 'inode', 'mode', 'links', 'mtime_ns', 'ctime_ns'})
BINDINGS = ('session_id', 'incarnation', 'node_id', 'master_id', 'relationship_id', 'scope')
HEADER_FIELDS = frozenset({'format', 'version', *BINDINGS, 'request_id', 'created_at', 'expires_at',
                           'root_exists', 'physical_free_bytes', 'physical_total_bytes', 'writable_verified'})


def entry(value):
    if not isinstance(value, dict) or value.keys() != ENTRY_FIELDS:
        raise InventoryError('inventory_entry_invalid')
    path = value['path']
    if (not isinstance(path, str) or not path or path.startswith('/') or
            any(part in {'', '.', '..'} for part in path.split('/')) or '\\' in path or '\x00' in path):
        raise InventoryError('inventory_entry_path_invalid')
    try:
        if len(path.encode('utf-8')) > 1024:
            raise ValueError()
    except (UnicodeError, ValueError) as exc:
        raise InventoryError('inventory_entry_path_invalid') from exc
    if not isinstance(value['kind'], str) or value['kind'] not in {'file', 'directory', 'symlink', 'other'} or any(
            type(value[name]) is not int or value[name] < 0
            for name in ENTRY_FIELDS - {'path', 'kind', 'mtime_ns', 'ctime_ns'}) or any(
            type(value[name]) is not int for name in ('mtime_ns', 'ctime_ns')):
        raise InventoryError('inventory_entry_metadata_invalid')
    if value['kind'] != 'file' and value['bytes'] != 0:
        raise InventoryError('inventory_entry_size_invalid')


class InventoryClient:
    def __init__(self, store, relation, transport, *, clock=time.monotonic, wall=time.time):
        self.store, self.relation, self.transport = store, relation, transport
        self.clock, self.wall = clock, wall

    async def _request(self, action, value):
        return await signed_request(self.store, self.relation, self.transport,
            '/internal/v1/migration/inventory/' + action, value,
            limit=PAGE_BYTES + 1024, error_type=InventoryError, prefix='inventory')

    def _header(self, value, request_id, scope):
        if (value.keys() != HEADER_FIELDS or value['format'] != 'frontiercloud-python-inventory' or
                type(value['version']) is not int or value['version'] != 1 or
                value['node_id'] != self.relation['peer_id'] or value['master_id'] != self.store.node['node_id'] or
                value['relationship_id'] != self.relation['relationship_id'] or
                value['request_id'] != request_id or value['scope'] != scope or
                not isinstance(value['session_id'], str) or not p.IDENTIFIER.fullmatch(value['session_id']) or
                not isinstance(value['incarnation'], str) or not p.IDENTIFIER.fullmatch(value['incarnation']) or
                any(type(value[name]) is not int or value[name] < 0 for name in
                    ('created_at', 'expires_at', 'physical_free_bytes', 'physical_total_bytes')) or
                type(value['root_exists']) is not bool or value['writable_verified'] is not False or
                value['physical_free_bytes'] > value['physical_total_bytes'] or
                value['expires_at'] - value['created_at'] != SESSION_SECONDS or
                abs(value['created_at'] - self.wall()) > p.AUTH_SKEW_SECONDS):
            raise InventoryError('inventory_header_invalid')

    async def read(self, scope, observe):
        if (self.store.node.get('role') != 'Master' or self.relation.get('state') != 'active' or
                self.relation.get('direction') != 'downstream' or
                not isinstance(scope, str) or scope not in {'data', 'media', 'recordings'}):
            raise InventoryError('inventory_member_invalid')
        request_id, session_id = uuid.uuid4().hex, None
        try:
            header = await self._request('start', dict(request_id=request_id, scope=scope))
            self._header(header, request_id, scope)
            session_id = header['session_id']
            transcript = hashlib.sha256(p.canonical(header))
            count = size = sequence = 0
            deadline = self.clock() + max(0, min(SESSION_SECONDS, header['expires_at'] - self.wall()))
            async with asyncio.timeout(max(0, deadline-self.clock())):
                while True:
                    page = await self._request('page', dict(session_id=session_id, sequence=sequence))
                    if (any(page.get(key) != header[key] for key in BINDINGS) or
                            type(page.get('sequence')) is not int or page['sequence'] != sequence):
                        raise InventoryError('inventory_page_binding_invalid')
                    if page.get('waiting_low_traffic') is True:
                        if page.keys() != {*BINDINGS, 'sequence', 'waiting_low_traffic'}:
                            raise InventoryError('inventory_wait_invalid')
                        await asyncio.sleep(1)
                        continue
                    if (page.keys() != {*BINDINGS, 'sequence', 'entries', 'complete', 'count', 'bytes', 'transcript_sha256'} or
                            type(page['complete']) is not bool or not isinstance(page['entries'], list) or
                            len(page['entries']) > PAGE_COUNT or type(page['count']) is not int or
                            type(page['bytes']) is not int):
                        raise InventoryError('inventory_page_invalid')
                    names = set()
                    for value in page['entries']:
                        entry(value)
                        if value['path'] in names:
                            raise InventoryError('inventory_duplicate_entry')
                        names.add(value['path'])
                        raw = p.canonical(value)
                        transcript.update(len(raw).to_bytes(4, 'big'))
                        transcript.update(raw)
                        count += 1
                        size += value['bytes']
                    if (count != page['count'] or size != page['bytes'] or
                            transcript.hexdigest() != page['transcript_sha256']):
                        raise InventoryError('inventory_transcript_invalid')
                    # The consumer persists observation paths with uniqueness
                    # constraints across pages. These writes grant no authority;
                    # no observation can become ready until completion verifies.
                    for value in page['entries']:
                        await observe(value)
                    sequence += 1
                    if page['complete']:
                        return dict(header=header, count=count, bytes=size, transcript_sha256=transcript.hexdigest())
        finally:
            if session_id is not None:
                # Closing is best effort. The server's independent reaper is
                # responsible for releasing abandoned fixed-lifetime sessions.
                try:
                    async with asyncio.timeout(2):
                        await self._request('close', dict(session_id=session_id))
                except (p.ProtocolError, httpx.HTTPError, OSError, TimeoutError):
                    pass
