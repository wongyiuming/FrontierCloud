"""Consume only completed, fresh source-content proofs bound to exact objects."""
import asyncio
import stat
import re
import time
import uuid

import httpx

from app.services.federation import protocol as p
from app.services.resource_control_client import signed_request
from app.services.resource_observation import LIFETIME, ObservationError, digest

BINDINGS = ('observation_id', 'incarnation', 'node_id', 'master_id', 'relationship_id',
            'request_id', 'resource_kind', 'object_id', 'user_id', 'created_at', 'expires_at')
BASE = frozenset({'format', 'version', *BINDINGS, 'state', 'observed_bytes', 'error_code'})
RESULT = frozenset({'physical_path', 'bytes', 'metadata_sha256', 'encryption_sha256', 'native',
                   'source_version', 'sha256', 'observed_at'})


class ObservationClient:
    def __init__(self, store, relation, transport, *, wall=time.time):
        self.store, self.relation, self.transport, self.wall = store, relation, transport, wall

    async def _request(self, action, value):
        return await signed_request(self.store, self.relation, self.transport,
            '/internal/v1/migration/observe/' + action, value,
            limit=16*1024, error_type=ObservationError, prefix='observation')

    def _validate(self, value, binding, initial=None):
        if (value.keys() != (BASE | {'result'} if value.get('state') == 'completed' else BASE) or
                value.get('format') != 'frontiercloud-python-content-observation' or
                type(value.get('version')) is not int or value['version'] != 1 or
                any(value.get(key) != expected for key, expected in binding.items()) or
                any(not isinstance(value.get(key), str) or not p.IDENTIFIER.fullmatch(value[key])
                    for key in ('observation_id', 'incarnation')) or
                any(type(value.get(key)) is not int or value[key] < 0 for key in ('created_at', 'expires_at', 'observed_bytes')) or
                value['expires_at'] - value['created_at'] != LIFETIME or self.wall() >= value['expires_at'] or
                value['created_at'] > self.wall() + p.AUTH_SKEW_SECONDS or
                not isinstance(value.get('state'), str) or value['state'] not in {'queued', 'waiting_low_traffic', 'observing', 'completed', 'failed', 'cancelled'} or
                not isinstance(value.get('error_code'), str) or re.fullmatch(r'[a-z0-9_]{0,64}', value['error_code']) is None or
                (initial is None and self.wall()-value['created_at'] > p.AUTH_SKEW_SECONDS)):
            raise ObservationError('observation_reply_invalid')
        if initial is not None and (any(value[key] != initial[key] for key in BINDINGS) or
                                    value['observed_bytes'] < initial['observed_bytes']):
            raise ObservationError('observation_binding_changed')
        if value['state'] != 'completed':
            return
        result = value['result']
        if (not isinstance(result, dict) or result.keys() != RESULT or
                type(result['bytes']) is not int or result['bytes'] <= 0 or
                value['observed_bytes'] != result['bytes'] or value['error_code'] or
                type(result['observed_at']) is not int or not value['created_at'] <= result['observed_at'] <= self.wall()+p.AUTH_SKEW_SECONDS or
                any(not isinstance(result[key], str) or not p.OBJECT_ID.fullmatch(result[key])
                    for key in ('sha256', 'metadata_sha256', 'encryption_sha256', 'source_version'))):
            raise ObservationError('observation_result_invalid')
        native = result['native']
        if (not isinstance(native, dict) or native.keys() != {'bytes', 'device', 'inode', 'mtime_ns', 'ctime_ns', 'mode', 'links'} or
                any(type(number) is not int for number in native.values()) or native['links'] != 1 or
                native['bytes'] != result['bytes'] or not 0 <= native['mode'] <= 65535 or
                native['device'] < 0 or native['inode'] < 0 or not stat.S_ISREG(native['mode'])):
            raise ObservationError('observation_native_identity_invalid')
        path = result['physical_path']
        if not isinstance(path, str) or len(path.encode('utf-8')) > 1024:
            raise ObservationError('observation_path_invalid')
        if binding['resource_kind'] == 'media':
            from app.services.resource_pool import validate_media_path
            validate_media_path(path)
        elif path != binding['relationship_id']+'/'+binding['user_id']+'/'+binding['object_id']+'.bin':
            raise ObservationError('observation_path_invalid')
        identity = {key: result[key] for key in ('physical_path', 'bytes', 'metadata_sha256', 'encryption_sha256', 'native')}
        if digest(identity) != result['source_version']:
            raise ObservationError('observation_version_invalid')

    async def read(self, resource_kind, object_id, user_id=''):
        if (self.store.node.get('role') != 'Master' or self.relation.get('state') != 'active' or
                self.relation.get('direction') != 'downstream'):
            raise ObservationError('observation_member_invalid')
        request = dict(request_id=uuid.uuid4().hex, resource_kind=resource_kind, object_id=object_id, user_id=user_id)
        binding = dict(**request, node_id=self.relation['peer_id'], master_id=self.store.node['node_id'],
                       relationship_id=self.relation['relationship_id'])
        observation_id = None
        try:
            value = await self._request('start', request)
            self._validate(value, binding)
            observation_id = value['observation_id']
            first = dict(value)
            async with asyncio.timeout(max(0, min(LIFETIME, value['expires_at']-self.wall()))):
                while True:
                    self._validate(value, binding, first)
                    if value['state'] == 'completed':
                        return value
                    if value['state'] in {'failed', 'cancelled'}:
                        raise ObservationError(value['error_code'] or 'observation_failed')
                    first = dict(value)
                    await asyncio.sleep(.1)
                    value = await self._request('status', {'observation_id': observation_id})
        finally:
            if observation_id is not None:
                try:
                    async with asyncio.timeout(2):
                        await self._request('close', {'observation_id': observation_id})
                except (p.ProtocolError, httpx.HTTPError, OSError, TimeoutError):
                    pass
