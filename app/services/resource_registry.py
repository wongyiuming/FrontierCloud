"""Bounded native registration roster, independently of the filesystem walk."""
import asyncio
import hashlib
from sqlalchemy import func, select, text

from app.services.federation import protocol as p, schema as fs
from app.services import resource_migration_schema as migration
from app.services.resource_observation import digest
from app.services.resource_control_client import signed_request

BATCH = 16


class RegistryError(p.ProtocolError):
    pass


class NativeRegistry:
    def __init__(self, store, observer, inventory):
        self.store, self.observer, self.inventory = store, observer, inventory

    async def page(self, node, relation, request_id, after):
        return await self._page(node,relation,request_id,after)

    async def reserved_page(self,node,relation,request_id,after,task_id,binding_sha256):
        if (not isinstance(task_id,str) or not p.IDENTIFIER.fullmatch(task_id) or
                not isinstance(binding_sha256,str) or not p.OBJECT_ID.fullmatch(binding_sha256)):
            raise RegistryError('registry_reservation_invalid')
        return await self._page(node,relation,request_id,after,task_id=task_id,binding_sha256=binding_sha256)

    async def _page(self,node,relation,request_id,after,*,task_id=None,binding_sha256=None):
        owner = self.observer._owner(node, relation)
        if (not isinstance(request_id, str) or not p.IDENTIFIER.fullmatch(request_id) or
                not isinstance(after, str) or (after and not p.OBJECT_ID.fullmatch(after))):
            raise RegistryError('registry_request_invalid')
        base = dict(format='frontiercloud-python-native-registry', version=1, **owner,
            request_id=request_id, after=after, incarnation=self.observer.incarnation,
            inventory_incarnation=self.inventory.incarnation)
        if task_id:
            base.update(task_id=task_id,binding_sha256=binding_sha256)
        if not self.observer.traffic.snapshot()['can_run']:
            return dict(**base, waiting_low_traffic=True)
        from app.services.media_manager import media_mutation_lock
        async with media_mutation_lock.shared():
            async with self.store.database.begin() as conn:
                owned_reserved=0
                if task_id:
                    from app.services.resource_peer_reservations import NativeReservations
                    row=await NativeReservations(self.store)._row(conn,node,relation,task_id)
                    if row['state']!='prepared' or row['binding_sha256']!=binding_sha256:
                        raise RegistryError('registry_reservation_invalid')
                    owned_reserved=row['expected_bytes'] if row['side']=='target' else 0
                identifiers = list((await conn.execute(text(
                    'SELECT media_id FROM media_objects WHERE media_id>:after ORDER BY media_id LIMIT 17'
                ), {'after': after})).scalars())
                uploads = await conn.scalar(select(func.count()).select_from(fs.upload_sessions).where(fs.upload_sessions.c.state == 'reserved'))
                staged = await conn.scalar(select(func.count()).select_from(migration.storage_journal).where(
                    migration.storage_journal.c.state.in_(['receiving','verified','publishing','deleting','aborting'])))
                tasks = await conn.scalar(select(func.count()).select_from(migration.tasks).where(
                    migration.tasks.c.state.not_in(['completed','cancelled'])))
                reserved = await conn.scalar(select(func.count()).select_from(migration.peer_reservations).where(
                    migration.peer_reservations.c.state.not_in(['aborted','released']),
                    migration.peer_reservations.c.task_id != task_id if task_id else True))
                quota=await conn.scalar(select(fs.storage_members.c.reserved_bytes).where(fs.storage_members.c.member_id==node['node_id']))
                # Recording receives reserve the member counter without a media
                # upload-session row. Their live writers must still block a scan.
                unaccounted=int(quota or 0)!=owned_reserved
            entries = []
            for identifier in identifiers[:BATCH]:
                if not isinstance(identifier, str) or not p.OBJECT_ID.fullmatch(identifier):
                    raise RegistryError('registry_identity_invalid')
                path, metadata, encryption = await self.observer.media_snapshot(identifier)
                entries.append(dict(object_id=identifier, path=path, object_kind=metadata['object_kind'],
                    metadata_sha256=digest(metadata), encryption_sha256=digest(encryption)))
        return dict(**base, entries=entries, complete=len(identifiers) <= BATCH,
            next=entries[-1]['object_id'] if entries else after,
            pending_operations=int(uploads or 0)+int(staged or 0)+int(tasks or 0)+int(reserved or 0)+int(unaccounted))


class RegistryClient:
    def __init__(self, store, relation, transport):
        self.store, self.relation, self.transport = store, relation, transport

    async def read(self, observe, *, reservation=None):
        if (self.store.node.get('role') != 'Master' or self.relation.get('state') != 'active' or
                self.relation.get('direction') != 'downstream'):
            raise RegistryError('registry_member_invalid')
        import uuid
        request_id, after, count = uuid.uuid4().hex, '', 0
        transcript = hashlib.sha256()
        epoch = None
        async with asyncio.timeout(300):
            while True:
                own = {} if reservation is None else dict(task_id=reservation['task_id'],binding_sha256=digest(reservation))
                value = await signed_request(self.store, self.relation, self.transport,
                    '/internal/v1/migration/registry/'+('reserved-media' if own else 'media'),
                    {'request_id': request_id, 'after': after, **own},
                    limit=48*1024, error_type=RegistryError, prefix='registry')
                base = dict(format='frontiercloud-python-native-registry', version=1,
                    node_id=self.relation['peer_id'], master_id=self.store.node['node_id'],
                    relationship_id=self.relation['relationship_id'], request_id=request_id, after=after, **own)
                if type(value.get('version')) is not int or any(value.get(name) != expected for name, expected in base.items()):
                    raise RegistryError('registry_binding_invalid')
                current = {name: value.get(name) for name in ('incarnation','inventory_incarnation')}
                if any(not isinstance(item, str) or not p.IDENTIFIER.fullmatch(item) for item in current.values()):
                    raise RegistryError('registry_incarnation_invalid')
                if epoch is None:
                    epoch = current
                elif current != epoch:
                    raise RegistryError('registry_incarnation_changed')
                if value.get('waiting_low_traffic') is True:
                    if value.keys() != {*base, *current, 'waiting_low_traffic'}:
                        raise RegistryError('registry_wait_invalid')
                    await asyncio.sleep(1)
                    continue
                if (value.keys() != {*base, *current, 'entries','complete','next','pending_operations'} or
                        type(value.get('complete')) is not bool or not isinstance(value.get('entries'), list) or
                        len(value['entries']) > BATCH or type(value.get('pending_operations')) is not int or
                        value['pending_operations'] != 0):
                    raise RegistryError('registry_pending_or_invalid')
                for item in value['entries']:
                    if (not isinstance(item, dict) or item.keys() != {'object_id','path','object_kind','metadata_sha256','encryption_sha256'} or
                            not isinstance(item['object_id'], str) or not p.OBJECT_ID.fullmatch(item['object_id']) or item['object_id'] <= after or
                            not isinstance(item['path'],str) or not isinstance(item['object_kind'],str) or
                            any(not isinstance(item[name], str) or not p.OBJECT_ID.fullmatch(item[name]) for name in ('metadata_sha256','encryption_sha256'))):
                        raise RegistryError('registry_entry_invalid')
                    from app.services.resource_pool import validate_media_path
                    path, kind = validate_media_path(item['path'])
                    if kind != item['object_kind']:
                        raise RegistryError('registry_kind_invalid')
                    raw = p.canonical(item)
                    transcript.update(len(raw).to_bytes(4,'big'))
                    transcript.update(raw)
                    count += 1
                    after = item['object_id']
                if value['next'] != after or (not value['complete'] and not value['entries']):
                    raise RegistryError('registry_cursor_invalid')
                for item in value['entries']:
                    await observe(item)
                if value['complete']:
                    return dict(**epoch, count=count, sha256=transcript.hexdigest())
