"""Read-only, paged native inventory with private, fixed-lifetime sessions.

Only metadata is observed here. File bytes, hashes, database ownership and
migration authority require additional verified steps in the planner/runner.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
import time
import uuid

from app.core.joined_io import joined_io
from app.services.federation import protocol as p

PAGE_COUNT = 64
PAGE_BYTES = 48 * 1024
SESSION_SECONDS = 300
MAX_DEPTH = 64


class InventoryError(p.ProtocolError):
    pass


def roots():
    from app.services.media_manager import MEDIA_ROOT
    from app.services.karaoke_storage import ROOT
    return {'data': MEDIA_ROOT.parent, 'media': MEDIA_ROOT, 'recordings': ROOT}


def _version(info):
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def _root(path):
    """Open every existing component without following links; never mkdir."""
    path = Path(path).absolute()
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                return descriptor, False
            os.close(descriptor)
            descriptor = child
        return descriptor, True
    except BaseException:
        os.close(descriptor)
        raise


class Walker:
    def __init__(self, path):
        self.path = Path(path)
        self.frames = []
        self.pending = None
        self.finished = False
        descriptor, self.exists = _root(path)
        try:
            usage = os.fstatvfs(descriptor)
            self.free_bytes = usage.f_bavail * usage.f_frsize
            self.total_bytes = usage.f_blocks * usage.f_frsize
            if self.exists:
                self.root_version = _version(os.fstat(descriptor))
                self.frames.append((descriptor, os.scandir(descriptor), '', self.root_version))
            else:
                self.root_version = None
                os.close(descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    def close(self):
        self.pending = None
        while self.frames:
            descriptor, iterator, _, _ = self.frames.pop()
            try:
                iterator.close()
            finally:
                os.close(descriptor)

    def _next(self):
        if not self.frames:
            return None
        descriptor, iterator, parent, version = self.frames[-1]
        try:
            entry = next(iterator)
        except StopIteration:
            if _version(os.fstat(descriptor)) != version:
                raise InventoryError('inventory_directory_changed')
            self.frames.pop()
            iterator.close()
            os.close(descriptor)
            return None
        path = parent + '/' + entry.name if parent else entry.name
        try:
            if len(path.encode('utf-8')) > 1024 or any(char in path for char in ('\x00', '\\')):
                raise ValueError()
        except (UnicodeError, ValueError) as exc:
            raise InventoryError('inventory_path_invalid') from exc
        info = entry.stat(follow_symlinks=False)
        kind = ('directory' if stat.S_ISDIR(info.st_mode) else
                'symlink' if stat.S_ISLNK(info.st_mode) else
                'file' if stat.S_ISREG(info.st_mode) else 'other')
        result = dict(path=path, kind=kind, bytes=info.st_size if kind == 'file' else 0,
                      device=info.st_dev, inode=info.st_ino, mode=stat.S_IMODE(info.st_mode),
                      links=info.st_nlink, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
        if kind == 'directory':
            if len(self.frames) >= MAX_DEPTH:
                raise InventoryError('inventory_depth_exceeded')
            child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                if _version(os.fstat(child)) != _version(info):
                    raise InventoryError('inventory_directory_changed')
                self.frames.append((child, os.scandir(child), path, _version(info)))
            except BaseException:
                os.close(child)
                raise
        return result

    def page(self):
        if self.finished:
            raise InventoryError('inventory_already_complete')
        entries, length, steps = [], 0, 0
        while steps < PAGE_COUNT * 2 and len(entries) < PAGE_COUNT:
            steps += 1
            value = self.pending
            self.pending = None
            if value is None:
                value = self._next()
            if value is not None:
                size = len(p.canonical(value)) + 1
                if length + size > PAGE_BYTES - 2048:
                    self.pending = value
                    break
                entries.append(value)
                length += size
            if not self.frames and self.pending is None:
                descriptor, exists = _root(self.path)
                try:
                    if exists != self.exists or (exists and _version(os.fstat(descriptor)) != self.root_version):
                        raise InventoryError('inventory_root_changed')
                finally:
                    os.close(descriptor)
                self.finished = True
                break
        return entries, self.finished


@dataclass
class Session:
    header: dict
    deadline: float
    walker: Walker
    transcript: object
    sequence: int = 0
    count: int = 0
    bytes: int = 0
    cached: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Inventory:
    def __init__(self, *, root_provider=roots, traffic=None, clock=time.monotonic, wall=time.time):
        from app.services.business_traffic import monitor
        self.root_provider = root_provider
        self.traffic = monitor if traffic is None else traffic
        self.clock, self.wall = clock, wall
        self.incarnation = uuid.uuid4().hex
        self.session = None
        self.lock = asyncio.Lock()

    def _owner(self, node, relation):
        if (node.get('role') != 'Follower' or relation.get('direction') != 'upstream' or
                relation.get('state') != 'active'):
            raise InventoryError('inventory_requires_active_master_relationship')
        owner = dict(node_id=node['node_id'], master_id=relation['peer_id'],
                     relationship_id=relation['relationship_id'])
        if any(not isinstance(value, str) or not p.IDENTIFIER.fullmatch(value) for value in owner.values()):
            raise InventoryError('inventory_identity_invalid')
        return owner

    async def start(self, node, relation, request_id, scope):
        owner = self._owner(node, relation)
        if (not isinstance(request_id, str) or not p.IDENTIFIER.fullmatch(request_id) or
                not isinstance(scope, str) or scope not in {'data', 'media', 'recordings'}):
            raise InventoryError('inventory_request_invalid')
        async with self.lock:
            await self._expire()
            if self.session is not None:
                header = self.session.header
                if all(header[key] == value for key, value in {**owner, 'request_id': request_id, 'scope': scope}.items()):
                    return dict(header)
                raise InventoryError('inventory_busy')
            walker = await joined_io(Walker, self.root_provider()[scope], on_cancel=lambda value: value.close())
            header = dict(format='frontiercloud-python-inventory', version=1, **owner,
                incarnation=self.incarnation, session_id=uuid.uuid4().hex, request_id=request_id,
                scope=scope, created_at=int(self.wall()), expires_at=int(self.wall()) + SESSION_SECONDS,
                root_exists=walker.exists, physical_free_bytes=walker.free_bytes,
                physical_total_bytes=walker.total_bytes, writable_verified=False)
            self.session = Session(header, self.clock()+SESSION_SECONDS, walker,
                                   hashlib.sha256(p.canonical(header)))
            return dict(header)

    async def _expire(self):
        if self.session is not None and self.clock() >= self.session.deadline:
            session = self.session
            async with session.lock:
                self.session = None
                await joined_io(session.walker.close)

    async def _get(self, node, relation, session_id):
        owner = self._owner(node, relation)
        async with self.lock:
            await self._expire()
            session = self.session
            if session is None or session.header['session_id'] != session_id or any(
                    session.header[key] != value for key, value in owner.items()):
                raise InventoryError('inventory_session_unavailable')
            return session

    async def page(self, node, relation, session_id, sequence):
        if type(sequence) is not int or sequence < 0:
            raise InventoryError('inventory_sequence_invalid')
        session = await self._get(node, relation, session_id)
        try:
            async with session.lock:
                if session is not self.session or self.clock() >= session.deadline:
                    raise InventoryError('inventory_session_unavailable')
                if session.cached is not None and sequence == session.sequence - 1:
                    return session.cached
                if sequence != session.sequence or session.walker.finished:
                    raise InventoryError('inventory_sequence_invalid')
                base = {key: session.header[key] for key in (
                    'session_id', 'incarnation', 'node_id', 'master_id', 'relationship_id', 'scope')}
                if not self.traffic.snapshot()['can_run']:
                    return dict(**base, sequence=sequence, waiting_low_traffic=True)
                entries, done = await joined_io(session.walker.page)
                for entry in entries:
                    encoded = p.canonical(entry)
                    session.transcript.update(len(encoded).to_bytes(4, 'big'))
                    session.transcript.update(encoded)
                    session.bytes += entry['bytes']
                    session.count += 1
                result = dict(**base, sequence=sequence, entries=entries, complete=done,
                    count=session.count, bytes=session.bytes, transcript_sha256=session.transcript.hexdigest())
                session.sequence += 1
                session.cached = result
                return result
        except (OSError, InventoryError, asyncio.CancelledError):
            await self.close(node, relation, session_id)
            raise

    async def close(self, node, relation, session_id):
        owner = self._owner(node, relation)
        async with self.lock:
            session = self.session
            if session is None:
                return
            if session.header['session_id'] != session_id or any(session.header[key] != value for key, value in owner.items()):
                raise InventoryError('inventory_session_unavailable')
            async with session.lock:
                self.session = None
                await joined_io(session.walker.close)

    async def stop(self):
        async with self.lock:
            session = self.session
            if session is not None:
                async with session.lock:
                    self.session = None
                    await joined_io(session.walker.close)

    async def reap(self):
        try:
            while True:
                await asyncio.sleep(1)
                async with self.lock:
                    await self._expire()
        finally:
            await self.stop()


inventory = Inventory()
