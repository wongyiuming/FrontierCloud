"""One joined, expiring source-content observation; bytes never leave the node.

Only managed media IDs or the current Master's fixed recording namespace can
select a file. Hashing is incremental and cooperates with live traffic. This
preview does not authorize migration, publication, deletion or decryption.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
import time
import uuid

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.joined_io import joined_io
from app.services.federation import protocol as p, schema as fs
from app.services.resource_inventory import Inventory, roots

CHUNK_BYTES = 64 * 1024
LIFETIME = 300
RELATION_FIELDS = ('relationship_id', 'peer_id', 'peer_key', 'peer_endpoint', 'credential', 'direction', 'state', 'protocol')


class ObservationError(p.ProtocolError):
    pass


def digest(value):
    return hashlib.sha256(p.canonical(value)).hexdigest()


def native(info):
    return dict(bytes=info.st_size, device=info.st_dev, inode=info.st_ino,
                mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns,
                mode=info.st_mode, links=info.st_nlink)


@dataclass
class Observation:
    header: dict
    relation: dict
    deadline: float
    task: object = None
    state: str = 'queued'
    observed_bytes: int = 0
    result: dict | None = None
    error_code: str = ''


class ContentObserver:
    def __init__(self, store, *, root_provider=roots, traffic=None, clock=time.monotonic, wall=time.time):
        from app.services.business_traffic import monitor
        self.store, self.root_provider = store, root_provider
        self.traffic = monitor if traffic is None else traffic
        self.clock, self.wall = clock, wall
        self.incarnation = uuid.uuid4().hex
        self.current = None
        self.lock = asyncio.Lock()

    def _owner(self, node, relation):
        return Inventory._owner(self, node, relation)

    async def _authorize(self, record):
        async with self.store.database.connect() as conn:
            node = (await conn.execute(select(fs.identity.c.node_id, fs.identity.c.role).where(
                fs.identity.c.singleton == 1))).mappings().first()
            relation = (await conn.execute(select(*(fs.relationships.c[name] for name in RELATION_FIELDS)).where(
                fs.relationships.c.relationship_id == record.header['relationship_id']))).mappings().first()
        if (node is None or node['role'] != 'Follower' or node['node_id'] != record.header['node_id'] or
                relation is None or any(relation[name] != record.relation[name] for name in RELATION_FIELDS)):
            raise ObservationError('observation_relationship_changed')

    def _budget(self, record):
        if self.clock() >= record.deadline:
            raise ObservationError('observation_expired')

    async def _quiet(self, record):
        while not self.traffic.snapshot()['can_run']:
            record.state = 'waiting_low_traffic'
            self._budget(record)
            await self._authorize(record)
            await asyncio.sleep(1)
        self._budget(record)
        await self._authorize(record)
        record.state = 'observing'

    async def media_snapshot(self, object_id, *, conn=None):
        from app.services.media_crypto import Metadata
        from app.services.media_crypto_schema import encryption
        from app.services.resource_pool import validate_media_path
        if conn is None:
            async with self.store.database.connect() as opened:return await self.media_snapshot(object_id,conn=opened)
        row = (await conn.execute(text('SELECT * FROM media_objects WHERE media_id=:id'),
            {'id': object_id})).mappings().first()
        encoded = (await conn.execute(select(encryption).where(encryption.c.object_kind == 'media',
            encryption.c.object_id == object_id))).mappings().first()
        if row is None:
            raise ObservationError('observation_media_unregistered')
        path, kind = validate_media_path(row['media_path'])
        if row['object_kind'] != kind or row['path_locator'] != hashlib.sha256(path.encode()).hexdigest():
            raise ObservationError('observation_media_identity_invalid')
        descriptor = None
        if encoded is not None:
            if encoded['descriptor_json'] is None:
                raise ObservationError('observation_media_retired')
            descriptor = Metadata.from_dict(json.loads(encoded['descriptor_json'])).to_dict()
            if descriptor['file_id'] != encoded['file_id']:
                raise ObservationError('observation_encryption_identity_invalid')
        metadata = dict(resource_kind='media', object_id=row['media_id'], physical_path=path,
                        object_kind=kind, created_at=str(row['created_at']), updated_at=str(row['updated_at']),
                        encryption=descriptor)
        return path, metadata, descriptor

    async def _footer(self, descriptor, size):
        from app.services import recording_crypto as recording
        suffix_length = len(recording.TRAILER_MAGIC) + 8
        if size < suffix_length:
            return {}
        suffix = await joined_io(os.pread, descriptor, suffix_length, size-suffix_length)
        if suffix[8:] != recording.TRAILER_MAGIC:
            return {}
        length = int.from_bytes(suffix[:8], 'big')
        if not 0 < length <= recording.MAX_METADATA_BYTES or length + suffix_length > size:
            raise ObservationError('observation_recording_footer_invalid')
        raw = await joined_io(os.pread, descriptor, length, size-suffix_length-length)
        if len(raw) != length:
            raise ObservationError('observation_recording_footer_invalid')
        return recording.metadata(json.loads(raw), with_version=True)

    async def _open(self, record):
        from app.middleware.native_storage import _open_beneath
        from app.services.media_manager import media_mutation_lock, ensure_media_mutations_ready
        await self._authorize(record)
        async with media_mutation_lock.shared():
            from fastapi import HTTPException
            try:
                ensure_media_mutations_ready()
            except HTTPException as exc:
                raise ObservationError('observation_media_recovery_required') from exc
            header = record.header
            if header['resource_kind'] == 'media':
                path, metadata, encryption = await self.media_snapshot(header['object_id'])
                root = self.root_provider()['media']
            else:
                path = header['relationship_id'] + '/' + header['user_id'] + '/' + header['object_id'] + '.bin'
                metadata = dict(resource_kind='recording', object_id=header['object_id'],
                                user_id=header['user_id'], physical_path=path)
                encryption = None
                root = self.root_provider()['recordings']
            descriptor, info = await joined_io(_open_beneath, root, path.split('/'),
                                                on_cancel=lambda value: os.close(value[0]))
            try:
                if info.st_nlink != 1 or info.st_size <= 0:
                    raise ObservationError('observation_resource_invalid')
                if encryption is not None and info.st_size != encryption['ciphertext_size']:
                    raise ObservationError('observation_ciphertext_size_invalid')
                if header['resource_kind'] == 'recording':
                    metadata['footer_sha256'] = digest(await self._footer(descriptor, info.st_size))
                identity = dict(physical_path=path, bytes=info.st_size,
                    metadata_sha256=digest(metadata), encryption_sha256=digest(encryption), native=native(info))
                identity['source_version'] = digest(identity)
                return descriptor, identity
            except BaseException:
                os.close(descriptor)
                raise

    async def _run(self, record):
        descriptor = None
        try:
            async with asyncio.timeout(LIFETIME):
                await self._quiet(record)
                descriptor, identity = await self._open(record)
                checksum = hashlib.sha256()
                offset = 0
                while offset < identity['bytes']:
                    await self._quiet(record)
                    value = await joined_io(os.pread, descriptor,
                        min(CHUNK_BYTES, identity['bytes'] - offset), offset)
                    if not value:
                        raise ObservationError('observation_resource_truncated')
                    checksum.update(value)
                    offset += len(value)
                    record.observed_bytes = offset
                    await asyncio.sleep(0)
                await self._quiet(record)
                if native(os.fstat(descriptor)) != identity['native']:
                    raise ObservationError('observation_resource_changed')
                latest, proof = await self._open(record)
                os.close(latest)
                if proof != identity:
                    raise ObservationError('observation_resource_changed')
                self._budget(record)
                record.result = {**identity, 'sha256': checksum.hexdigest(), 'observed_at': int(self.wall())}
                record.state = 'completed'
        except asyncio.CancelledError:
            record.state, record.error_code = 'cancelled', 'observation_cancelled'
            raise
        except TimeoutError:
            record.state, record.error_code = 'failed', 'observation_expired'
        except ObservationError as exc:
            record.state, record.error_code = 'failed', str(exc)
        except (p.ProtocolError, ValueError, KeyError, TypeError, RecursionError):
            record.state, record.error_code = 'failed', 'observation_metadata_invalid'
        except OSError:
            record.state, record.error_code = 'failed', 'observation_filesystem_unavailable'
        except SQLAlchemyError:
            record.state, record.error_code = 'failed', 'observation_database_unavailable'
        except Exception:
            record.state, record.error_code = 'failed', 'observation_internal_error'
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _view(self, record):
        result = dict(**record.header, state=record.state, observed_bytes=record.observed_bytes,
                      error_code=record.error_code)
        if record.state == 'completed':
            result['result'] = record.result
        return result

    async def start(self, node, relation, request_id, resource_kind, object_id, user_id):
        owner = self._owner(node, relation)
        if (not isinstance(request_id, str) or not p.IDENTIFIER.fullmatch(request_id) or
                not isinstance(object_id, str) or not isinstance(user_id, str) or
                not isinstance(resource_kind, str) or resource_kind not in {'media', 'recording'} or
                (resource_kind == 'media' and (not p.OBJECT_ID.fullmatch(object_id) or user_id)) or
                (resource_kind == 'recording' and (not p.IDENTIFIER.fullmatch(object_id) or not p.IDENTIFIER.fullmatch(user_id)))):
            raise ObservationError('observation_request_invalid')
        binding = dict(**owner, request_id=request_id, resource_kind=resource_kind, object_id=object_id, user_id=user_id)
        if self.lock.locked():
            raise ObservationError('observation_busy')
        async with self.lock:
            if self.current is not None:
                record = self.current
                if self.clock() >= record.deadline:
                    await self._stop(record)
                    self.current = None
                elif record.header['request_id'] == request_id:
                    if any(record.header[name] != value for name, value in binding.items()):
                        raise ObservationError('observation_request_changed')
                    await self._authorize(record)
                    return self._view(record)
                elif not record.task.done():
                    raise ObservationError('observation_busy')
            header = dict(format='frontiercloud-python-content-observation', version=1, **binding,
                          observation_id=uuid.uuid4().hex, incarnation=self.incarnation,
                          created_at=int(self.wall()), expires_at=int(self.wall())+LIFETIME)
            record = Observation(header, {name: relation[name] for name in RELATION_FIELDS}, self.clock()+LIFETIME)
            record.task = asyncio.create_task(self._run(record), name='native-content-observation')
            self.current = record
            return self._view(record)

    async def status(self, node, relation, observation_id):
        owner = self._owner(node, relation)
        if not isinstance(observation_id, str) or not p.IDENTIFIER.fullmatch(observation_id):
            raise ObservationError('observation_request_invalid')
        async with self.lock:
            record = self.current
            if (record is None or record.header['observation_id'] != observation_id or
                    any(record.header[name] != value for name, value in owner.items())):
                raise ObservationError('observation_unavailable')
            if self.clock() >= record.deadline:
                await self._stop(record)
                self.current = None
                raise ObservationError('observation_expired')
            try:
                await self._authorize(record)
            except ObservationError:
                await self._stop(record)
                self.current = None
                raise
            return self._view(record)

    async def _stop(self, record):
        record.task.cancel()
        try:
            await record.task
        except asyncio.CancelledError:
            pass

    async def close(self, node, relation, observation_id):
        owner = self._owner(node, relation)
        if not isinstance(observation_id, str) or not p.IDENTIFIER.fullmatch(observation_id):
            raise ObservationError('observation_request_invalid')
        async with self.lock:
            record = self.current
            if record is None:
                return {'closed': True, 'observation_id': observation_id}
            if record.header['observation_id'] != observation_id or any(
                    record.header[name] != value for name, value in owner.items()):
                raise ObservationError('observation_unavailable')
            await self._stop(record)
            self.current = None
            return {'closed': True, 'observation_id': observation_id}

    async def stop(self):
        async with self.lock:
            if self.current is not None:
                await self._stop(self.current)
                self.current = None


from app.services.federation.state import state
observer = ContentObserver(state)
