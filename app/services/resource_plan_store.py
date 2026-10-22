"""Bounded SQL spooling for read-only migration observations, never admission."""
import json
import time

from sqlalchemy import and_, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.services.federation import protocol as p, schema as fs
from app.services import karaoke_schema as ks, media_crypto_schema as crypto_schema
from app.services import resource_migration_schema as s
from app.services.media_crypto import Metadata
from app.services.resource_observation import digest
from app.services.resource_encryption_binding import bind_source
from app.services.resource_pool import _upsert, validate_media_path

BATCH = 16


class PlanError(p.ProtocolError):
    pass


def path_key(purpose, member_id, scope, path):
    return digest([purpose, member_id, scope, path])


def relation_binding(row):
    return digest({name: row[name] for name in ('relationship_id','peer_id','peer_endpoint','peer_key',
                                               'credential','direction','mode','state','protocol')})


class PlanStore:
    def __init__(self, store, *, wall=time.time):
        self.store, self.wall = store, wall

    async def facts(self, source_id, target_id, *, conn=None, task_id=None):
        if conn is None:
            async with self.store.database.connect() as opened:
                return await self.facts(source_id,target_id,conn=opened,task_id=task_id)
        if any(not isinstance(value, str) or not p.IDENTIFIER.fullmatch(value) for value in (source_id,target_id)) or source_id == target_id:
            raise PlanError('migration_members_invalid')
        node = (await conn.execute(select(fs.identity.c.node_id,fs.identity.c.role).where(fs.identity.c.singleton == 1))).mappings().one()
        if node['role'] != 'Master':
            raise PlanError('migration_requires_master')
        members, relations = {}, {}
        for identifier in (source_id,target_id):
            member = (await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id == identifier))).mappings().first()
            if member is None or member['member_kind'] != 'Follower':
                raise PlanError('migration_master_local_or_unknown')
            relation = (await conn.execute(select(fs.relationships).where(
                fs.relationships.c.relationship_id == member['relationship_id']))).mappings().first()
            if (relation is None or relation['peer_id'] != identifier or relation['direction'] != 'downstream' or
                    relation['state'] != 'active' or relation['status'] != 'online' or relation['mode'] not in {'Direct','Relay'} or
                    relation['protocol'] != p.PROTOCOL_VERSION or member['transport'] != relation['mode'] or
                    self.wall()-relation['last_heartbeat'] > p.OFFLINE_SECONDS or
                    not member['storage_enabled'] or member['health'] != 'online' or not member['writable']):
                raise PlanError('migration_member_not_ready')
            members[identifier], relations[identifier] = dict(member), dict(relation)
        pair = [source_id,target_id]
        for table, condition in (
            (fs.upload_sessions, and_(fs.upload_sessions.c.storage_member_id.in_(pair),fs.upload_sessions.c.state == 'reserved')),
            (fs.global_media, and_(fs.global_media.c.storage_member_id.in_(pair),fs.global_media.c.state != 'active')),
            (ks.recordings, and_(ks.recordings.c.storage_member_id.in_(pair),ks.recordings.c.state != 'ready')),
            (s.drains,and_(s.drains.c.member_id.in_(pair),s.drains.c.owner_id != task_id if task_id else True)),
            (s.scopes,and_(s.scopes.c.member_id.in_(pair),s.scopes.c.task_id != task_id if task_id else True)),
            (s.capacity,and_(s.capacity.c.member_id.in_(pair),s.capacity.c.task_id != task_id if task_id else True)),
            (s.tasks,and_((s.tasks.c.source_id.in_(pair)) | (s.tasks.c.target_id.in_(pair)),
                          s.tasks.c.state.not_in(['completed','cancelled']),
                          s.tasks.c.task_id != task_id if task_id else True)),
        ):
            if await conn.scalar(select(func.count()).select_from(table).where(condition)):
                raise PlanError('migration_unreconciled_operation')
        if await conn.scalar(text('SELECT COUNT(*) FROM media_delete_operations')):
            raise PlanError('migration_deletion_recovery_pending')
        if task_id:
            task = (await conn.execute(select(s.tasks).where(s.tasks.c.task_id == task_id))).mappings().first()
            capacity = (await conn.execute(select(s.capacity).where(s.capacity.c.task_id == task_id))).mappings().first()
            if (not task or task['master_id'] != node['node_id'] or task['source_id'] != source_id or
                    task['target_id'] != target_id or not capacity or capacity['member_id'] != target_id or
                    capacity['reserved_bytes'] != task['total_bytes'] or
                    members[target_id]['reserved_bytes'] < task['total_bytes'] or
                    not await conn.scalar(select(s.drains.c.owner_id).where(s.drains.c.member_id == source_id,
                        s.drains.c.owner_id == task_id,s.drains.c.reason == 'migration'))):
                raise PlanError('migration_admission_reservation_inconsistent')
            # Compare the same facts as the readonly preview, excluding only
            # this task's verified reservation. Unrelated writers still block.
            members[target_id]['reserved_bytes'] -= task['total_bytes']
        stable = dict(master_id=node['node_id'], source_id=source_id, target_id=target_id,
            relationships={identifier: relation_binding(value) for identifier,value in relations.items()},
            storage={identifier: {name: value[name] for name in ('member_id','member_kind','transport','storage_enabled',
                'allocated_bytes','used_bytes','reserved_bytes','health','writable')} for identifier,value in members.items()})
        return stable, members, relations

    async def catalog(self, source_id, source_relation, target_relation):
        for kind, table, identifier in (('media',fs.global_media,fs.global_media.c.media_id),
                                        ('recording',ks.recordings,ks.recordings.c.recording_id)):
            after = ''
            while True:
                async with self.store.database.connect() as conn:
                    query = select(table).where(table.c.storage_member_id == source_id,identifier > after).order_by(identifier).limit(BATCH)
                    rows = list((await conn.execute(query)).mappings())
                    entries = []
                    for raw in rows:
                        row = dict(raw)
                        logical_id = row['media_id'] if kind == 'media' else row['recording_id']
                        if kind == 'media':
                            if row['state'] != 'active' or row['size_bytes'] <= 0:
                                raise PlanError('migration_catalog_not_ready')
                            path, object_kind = validate_media_path(row['media_path'])
                            if row['object_kind'] != object_kind or not p.OBJECT_ID.fullmatch(row['object_id']):
                                raise PlanError('migration_catalog_identity_invalid')
                            encryption = (await conn.execute(select(crypto_schema.encryption).where(
                                crypto_schema.encryption.c.object_kind == 'media',
                                crypto_schema.encryption.c.object_id == logical_id))).mappings().first()
                            descriptor = None
                            if encryption is not None:
                                if encryption['descriptor_json'] is None:
                                    raise PlanError('migration_encryption_retired')
                                descriptor = Metadata.from_dict(json.loads(encryption['descriptor_json'])).to_dict()
                                if descriptor['file_id'] != encryption['file_id'] or descriptor['ciphertext_size'] != row['size_bytes']:
                                    raise PlanError('migration_encryption_identity_invalid')
                            parent = path.rsplit('/',1)[0]
                            prefix = parent+'/'
                            conflict = await conn.scalar(select(func.count()).select_from(fs.global_media).where(
                                fs.global_media.c.media_path.startswith(prefix,autoescape=True),
                                (fs.global_media.c.storage_member_id != source_id) |
                                (func.instr(func.substr(fs.global_media.c.media_path,len(prefix)+1),'/') > 0)))
                            if conflict:
                                raise PlanError('migration_parent_split_or_nested')
                            snapshot = dict(master=row,encryption=descriptor,physical_path=path,target_path=path,
                                parent_path=parent,target_parent=parent,source_object=row['object_id'])
                        else:
                            if (row['state'] != 'ready' or row['size_bytes'] <= 0 or not isinstance(row['sha256'],str) or
                                    not p.OBJECT_ID.fullmatch(row['sha256']) or not p.IDENTIFIER.fullmatch(row['user_id'])):
                                raise PlanError('migration_recording_not_ready')
                            from app.services.recording_crypto import decode_lyrics
                            _, _, pending = decode_lyrics(row['lyrics'])
                            if pending is not None:
                                raise PlanError('migration_recording_not_ready')
                            row['lyrics_sha256'], row['title_sha256'] = digest(row.pop('lyrics')), digest(row.pop('title'))
                            parent = source_relation+'/'+row['user_id']
                            target_parent = target_relation+'/'+row['user_id']
                            snapshot = dict(master=row,encryption=None,source_object=logical_id,
                                physical_path=parent+'/'+logical_id+'.bin',target_path=target_parent+'/'+logical_id+'.bin',
                                parent_path=parent,target_parent=target_parent)
                        if not isinstance(logical_id,str) or not (p.OBJECT_ID if kind == 'media' else p.IDENTIFIER).fullmatch(logical_id):
                            raise PlanError('migration_catalog_identity_invalid')
                        entries.append(dict(logical_id=logical_id,resource_kind=kind,snapshot=snapshot,
                            unit_id=digest([kind,source_id,snapshot['parent_path']]), expected_bytes=row['size_bytes']))
                if not rows:
                    break
                for value in entries:
                    yield value
                after = entries[-1]['logical_id']

    async def create(self, plan_id, facts, epoch, actor):
        if not isinstance(plan_id,str) or not p.IDENTIFIER.fullmatch(plan_id):
            raise PlanError('migration_request_invalid')
        header = dict(**facts, epoch=epoch, blockers=[], blocker_count=0, blockers_complete=False,
                      residual_count=0, writable_verified=False, stage='catalog', observed_objects=0)
        async with self.store.database.begin() as conn:
            node = await self.store.lock(conn)
            if node['node_id'] != facts['master_id'] or node['role'] != 'Master':
                raise PlanError('migration_master_changed')
            existing = (await conn.execute(select(s.plans).where(s.plans.c.plan_id == plan_id))).mappings().first()
            if existing is not None:
                if any(existing[key] != facts[key] for key in ('master_id','source_id','target_id')):
                    raise PlanError('migration_request_changed')
                raise PlanError('migration_plan_already_exists')
            await conn.execute(insert(s.plans).values(plan_id=plan_id, master_id=facts['master_id'],
                source_id=facts['source_id'],target_id=facts['target_id'],state='observing',
                header_json=p.canonical(header).decode(),created_at=int(self.wall()),expires_at=int(self.wall())+1800))
            await self.store.log(conn,'migration_plan_observe',actor,plan_id=plan_id,
                                 source_id=facts['source_id'],target_id=facts['target_id'])
        return header

    async def reap_previews(self):
        """Bounded removal of expired observation rows; never touch task state."""
        async with self.store.database.begin() as conn:
            node = await self.store.lock(conn)
            expired = list((await conn.execute(select(s.plans.c.plan_id).where(
                s.plans.c.master_id == node['node_id'],s.plans.c.state != 'admitted',
                s.plans.c.expires_at <= int(self.wall())).order_by(s.plans.c.expires_at).limit(4))).scalars())
            for plan_id in expired:
                # A future admission may retain its preview ID as task ID.
                if await conn.scalar(select(func.count()).select_from(s.tasks).where(s.tasks.c.task_id == plan_id)):
                    continue
                keys = list((await conn.execute(select(s.plan_paths.c.path_locator).where(
                    s.plan_paths.c.plan_id == plan_id).limit(256))).scalars())
                if keys:
                    await conn.execute(delete(s.plan_paths).where(s.plan_paths.c.plan_id == plan_id,
                        s.plan_paths.c.path_locator.in_(keys)))
                identifiers = list((await conn.execute(select(s.plan_bindings.c.logical_id).where(
                    s.plan_bindings.c.plan_id == plan_id).limit(BATCH))).scalars())
                if identifiers:
                    await conn.execute(delete(s.plan_bindings).where(s.plan_bindings.c.plan_id == plan_id,
                        s.plan_bindings.c.logical_id.in_(identifiers)))
                remaining = sum([int(await conn.scalar(select(func.count()).select_from(table).where(
                    table.c.plan_id == plan_id)) or 0) for table in (s.plan_paths,s.plan_bindings)])
                if not remaining:
                    await conn.execute(delete(s.plans).where(s.plans.c.plan_id == plan_id))

    async def _marker(self, conn, plan_id, kind, purpose, member_id, scope, path, entry_kind):
        values = dict(plan_id=plan_id,resource_kind=kind,path_locator=path_key(purpose,member_id,scope,path),entry_kind=entry_kind)
        await _upsert(conn,s.plan_paths,values,tuple(values),update_columns=('path_locator',))

    async def add(self, plan_id, entry, source_id, target_id):
        snapshot = entry['snapshot']
        kind = entry['resource_kind']
        scope = 'media' if kind == 'media' else 'recordings'
        async with self.store.database.begin() as conn:
            await conn.execute(insert(s.plan_bindings).values(plan_id=plan_id, logical_id=entry['logical_id'],
                unit_id=entry['unit_id'],resource_kind=kind,source_object=snapshot['source_object'],
                object_sha256=digest(snapshot),object_json=p.canonical(snapshot).decode(),expected_bytes=entry['expected_bytes']))
            for member,path in ((source_id,snapshot['physical_path']),(target_id,snapshot['target_path'])):
                await self._marker(conn,plan_id,kind,'expected',member,scope,path,'name')
                await self._marker(conn,plan_id,kind,'expected',member,scope,path.rsplit('/',1)[0],'parent')
                parts = path.split('/')
                for length in range(1,len(parts)):
                    await self._marker(conn,plan_id,kind,'expected',member,scope,'/'.join(parts[:length]),'ancestor')

    async def bindings(self, plan_id):
        for kind in ('media','recording'):
            after = ''
            while True:
                async with self.store.database.connect() as conn:
                    rows = list((await conn.execute(select(s.plan_bindings).where(s.plan_bindings.c.plan_id == plan_id,
                        s.plan_bindings.c.resource_kind == kind,s.plan_bindings.c.logical_id > after)
                        .order_by(s.plan_bindings.c.logical_id).limit(BATCH))).mappings())
                if not rows:
                    break
                for row in rows:
                    yield dict(row)
                after = rows[-1]['logical_id']

    async def marker(self, conn, plan_id, kind, member, scope, path, entry_kind):
        return bool(await conn.scalar(select(func.count()).select_from(s.plan_paths).where(s.plan_paths.c.plan_id == plan_id,
            s.plan_paths.c.resource_kind == kind,s.plan_paths.c.path_locator == path_key('expected',member,scope,path),
            s.plan_paths.c.entry_kind == entry_kind)))

    async def inside_parent(self, conn, plan_id, kind, member, scope, path):
        parts = path.split('/')
        keys = [path_key('expected',member,scope,'/'.join(parts[:index])) for index in range(1,len(parts))]
        return bool(keys and await conn.scalar(select(func.count()).select_from(s.plan_paths).where(
            s.plan_paths.c.plan_id == plan_id,s.plan_paths.c.resource_kind == kind,
            s.plan_paths.c.path_locator.in_(keys),s.plan_paths.c.entry_kind == 'parent')))

    async def native_entry(self, plan_id, member, source_id, source_relationship, scope, entry, *, observation_round='initial'):
        kind = 'recording' if scope == 'recordings' else 'media'
        if observation_round not in {'initial','final','admission'}:
            raise PlanError('migration_observation_round_invalid')
        # Commit uniqueness independently of classification. A conflicting entry
        # must not lose its duplicate marker when its classification rolls back.
        async with self.store.database.begin() as conn:
            try:
                await conn.execute(insert(s.plan_paths).values(plan_id=plan_id,resource_kind=kind,
                    path_locator=path_key('observed-'+observation_round,member,scope,entry['path']),
                    entry_kind='admission' if observation_round == 'admission' else 'name'))
            except IntegrityError as exc:
                raise PlanError('migration_native_duplicate') from exc
        async with self.store.database.begin() as conn:
            if scope == 'data':
                return None if entry['path'].split('/')[0] in {'media','recordings'} else 'residual'
            name = await self.marker(conn,plan_id,kind,member,scope,entry['path'],'name')
            parent = await self.inside_parent(conn,plan_id,kind,member,scope,entry['path'])
            ancestor = await self.marker(conn,plan_id,kind,member,scope,entry['path'],'ancestor')
            if member != source_id:
                if parent or name or (ancestor and entry['kind'] != 'directory'):
                    raise PlanError('migration_target_parent_occupied')
                return None
            if entry['kind'] == 'directory':
                return None
            if scope == 'recordings' and not entry['path'].startswith(source_relationship+'/'):
                return 'residual'
            if scope == 'media' and entry['path'] == 'lyrics/default.lrc':
                return 'residual'
            if not name or entry['kind'] != 'file' or entry['links'] != 1:
                raise PlanError('migration_source_unmanaged_or_unsupported')
            if kind == 'media':
                logical = await conn.scalar(select(fs.global_media.c.media_id).where(
                    fs.global_media.c.storage_member_id == member,fs.global_media.c.media_path == entry['path']))
            else:
                parts = entry['path'].split('/')
                logical = await conn.scalar(select(ks.recordings.c.recording_id).where(
                    ks.recordings.c.storage_member_id == member,ks.recordings.c.recording_id == parts[-1].removesuffix('.bin'),
                    ks.recordings.c.user_id == parts[-2]))
            row = (await conn.execute(select(s.plan_bindings).where(s.plan_bindings.c.plan_id == plan_id,
                s.plan_bindings.c.logical_id == logical,s.plan_bindings.c.resource_kind == kind))).mappings().first()
            if row is None or row['expected_bytes'] != entry['bytes']:
                raise PlanError('migration_native_catalog_mismatch')
            snapshot = json.loads(row['object_json'])
            current = digest({key: entry[key] for key in
                ('bytes','device','inode','mode','links','mtime_ns','ctime_ns')})
            if observation_round in {'final','admission'} and current != snapshot.get('native_entry_sha256'):
                raise PlanError('migration_source_changed_after_hash')
            snapshot['native_entry_sha256'] = current
            await conn.execute(update(s.plan_bindings).where(s.plan_bindings.c.plan_id == plan_id,
                s.plan_bindings.c.logical_id == logical).values(native_seen=1,object_json=p.canonical(snapshot).decode()))

    async def registration(self, plan_id, member, source_id, entry, *, verify=False):
        async with self.store.database.begin() as conn:
            row = (await conn.execute(select(s.plan_bindings).where(s.plan_bindings.c.plan_id == plan_id,
                s.plan_bindings.c.resource_kind == 'media',s.plan_bindings.c.source_object == entry['object_id']))).mappings().first()
            if member != source_id:
                if row is not None or await self.inside_parent(conn,plan_id,'media',member,'media',entry['path']):
                    raise PlanError('migration_target_registration_conflict')
                return
            if row is None:
                raise PlanError('migration_source_registration_unowned')
            snapshot = json.loads(row['object_json'])
            if snapshot['physical_path'] != entry['path']:
                raise PlanError('migration_source_registration_mismatch')
            try:snapshot=bind_source(snapshot,entry['encryption_sha256'])
            except ValueError:raise PlanError('migration_source_registration_mismatch') from None
            if verify:
                if row['native_metadata_sha256'] != entry['metadata_sha256']:
                    raise PlanError('migration_source_metadata_changed')
                return
            await conn.execute(update(s.plan_bindings).where(s.plan_bindings.c.plan_id == plan_id,
                s.plan_bindings.c.logical_id == row['logical_id']).values(native_metadata_sha256=entry['metadata_sha256'],
                    object_json=p.canonical(snapshot).decode()))

    async def save_header(self, plan_id, header, **values):
        async with self.store.database.begin() as conn:
            await conn.execute(update(s.plans).where(s.plans.c.plan_id == plan_id).values(
                header_json=p.canonical(header).decode(),**values))
