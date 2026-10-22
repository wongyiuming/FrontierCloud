"""Durable native migration preparation; no byte transfer or publication authority.

Only the current authenticated Master may reserve. Scopes and quota survive
restart, cancellation intent and age. Releasing an unstarted reservation is a
separate verified transaction and never removes any resource file.
"""
import json
import os
import time

from sqlalchemy import delete, func, insert, select, text, update

from app.core.joined_io import joined_io
from app.services import resource_fences as fences, resource_migration_schema as s
from app.services.federation import protocol as p, schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_inventory import _root, roots
from app.services.resource_observation import digest
from app.services.resource_pool import PHYSICAL_RESERVE_BYTES, validate_media_path

INITIAL_UNITS_SHA256=digest(['frontiercloud-python-migration-units',1])
REQUEST_FIELDS={'task_id','master_id','source_id','target_id','source_relationship','target_relationship',
    'side','plan_sha256','unit_count','object_count','total_bytes','units_sha256'}
UNIT_FIELDS={'unit_id','resource_kind','source_parent','target_parent','source_version','object_count','total_bytes'}


class ReservationError(p.ProtocolError):
    pass


def _identifier(value,*,hash_value=False):
    if not isinstance(value,str) or not (p.OBJECT_ID if hash_value else p.IDENTIFIER).fullmatch(value):
        raise ReservationError('migration_reservation_identity_invalid')
    return value


def _positive(value,maximum=2**53-1):
    if type(value) is not int or not 0<value<=maximum:
        raise ReservationError('migration_reservation_count_invalid')
    return value


def unit_transcript(previous,value):
    return digest(dict(previous=previous,unit=value))


def _capacity(root_provider):
    # A missing media/recording root is observed from its nearest existing
    # parent. No write probe, mkdir, file read, symlink following or free-space
    # sum occurs here. The eventual staging admission must prove writability.
    free=[]
    for scope in ('media','recordings'):
        descriptor,_=_root(root_provider()[scope])
        try:
            info=os.fstatvfs(descriptor)
            free.append(info.f_bavail*info.f_frsize)
        finally:
            os.close(descriptor)
    return min(free)


class NativeReservations:
    def __init__(self,store,*,root_provider=roots,wall=time.time):
        self.store,self.root_provider,self.wall=store,root_provider,wall

    async def _owner(self,conn,node,relation):
        current=await self.store.lock(conn)
        own=current['node_id']
        active=(await conn.execute(select(fs.relationships).where(
            fs.relationships.c.relationship_id==relation['relationship_id']).with_for_update())).mappings().first()
        if (current['role']!='Follower' or node.get('node_id')!=own or active is None or
            active['state']!='active' or active['direction']!='upstream' or
            active['protocol']!=p.PROTOCOL_VERSION or active['mode'] not in {'Direct','Relay'} or
            any(active[key]!=relation.get(key) for key in ('peer_id','peer_key','credential','peer_endpoint','direction','state','mode','protocol'))):
            raise ReservationError('migration_reservation_master_changed')
        binding=digest([own,current['endpoint'],active['relationship_id'],active['peer_id'],active['peer_key'],
                        active['credential'],active['peer_endpoint'],active['mode'],active['protocol']])
        return current,dict(active),binding

    async def _row(self,conn,node,relation,task_id):
        _identifier(task_id)
        current,active,binding=await self._owner(conn,node,relation)
        row=(await conn.execute(select(s.peer_reservations).where(
            s.peer_reservations.c.task_id==task_id).with_for_update())).mappings().first()
        if (row is None or row['member_id']!=current['node_id'] or row['master_id']!=active['peer_id'] or
            row['relationship_id']!=active['relationship_id'] or row['payload']['owner_binding']!=binding):
            raise ReservationError('migration_reservation_unavailable')
        row=dict(row)
        committed=int(await conn.scalar(select(func.coalesce(func.sum(s.units.c.total_bytes),0)).where(
            s.units.c.task_id==task_id,s.units.c.state.in_(['committed','cleanup_pending','cleaned']))))
        cancelled=int(await conn.scalar(select(func.coalesce(func.sum(s.units.c.total_bytes),0)).where(
            s.units.c.task_id==task_id,s.units.c.state=='cancelled',s.units.c.native_acknowledged==1)))
        if committed+cancelled>row['expected_bytes'] or (committed or cancelled) and row['state'] not in {'active','released'}:
            raise ReservationError('migration_reservation_committed_inconsistent')
        row['_committed_bytes']=committed;row['_cancelled_bytes']=cancelled
        if row['state'] in {'preparing','prepared','active','aborting'}:
            capacity=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==task_id))).mappings().first()
            reserved=await conn.scalar(select(fs.storage_members.c.reserved_bytes).where(fs.storage_members.c.member_id==row['member_id']))
            expected=row['expected_bytes']-committed-cancelled if row['side']=='target' else 0
            if (capacity is None or capacity['member_id']!=row['member_id'] or capacity['reserved_bytes']!=expected or
                reserved is None or reserved<expected):
                raise ReservationError('migration_reservation_capacity_inconsistent')
            if row['side']=='source' and not await conn.scalar(select(s.drains.c.owner_id).where(
                s.drains.c.member_id==row['member_id'],s.drains.c.owner_id==task_id,s.drains.c.reason=='migration')):
                raise ReservationError('migration_reservation_drain_inconsistent')
        return row

    def _status(self,row,request_id):
        transfer=row['payload'].get('transfer',{})
        active=row['state']=='active' and transfer.get('phase')=='copy' and transfer.get('expires_at',0)>self.wall()
        return dict(format='frontiercloud-python-native-reservation',version=1,request_id=request_id,
            node_id=row['member_id'],master_id=row['master_id'],task_id=row['task_id'],side=row['side'],state=row['state'],
            binding_sha256=row['binding_sha256'],revision=row['revision'],unit_count=row['accepted_units'],
            object_count=row['accepted_objects'],total_bytes=row['accepted_bytes'],units_sha256=row['units_sha256'],
            committed_bytes=row.get('_committed_bytes',0),cancelled_bytes=row.get('_cancelled_bytes',0),
            capacity_reserved=row['expected_bytes']-row.get('_committed_bytes',0)-row.get('_cancelled_bytes',0) if row['side']=='target' and row['state'] not in {'aborted','released'} else 0,
            writable_verified=bool(row['side']=='target' and row['state']=='active' and transfer.get('staging_roots')),
            execution_authorized=active)

    def _request(self,reservation):
        if not isinstance(reservation,dict) or reservation.keys()!=REQUEST_FIELDS:
            raise ReservationError('migration_reservation_request_invalid')
        for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship'):
            _identifier(reservation[name])
        for name in ('plan_sha256','units_sha256'):_identifier(reservation[name],hash_value=True)
        for name in ('unit_count','object_count'):_positive(reservation[name],2**31-1)
        _positive(reservation['total_bytes'])
        if (reservation['source_id']==reservation['target_id'] or reservation['side'] not in {'source','target'} or
            reservation['source_relationship']==reservation['target_relationship'] or
            reservation['master_id'] in {reservation['source_id'],reservation['target_id']} or
            reservation['unit_count']>reservation['object_count']):
            raise ReservationError('migration_reservation_request_invalid')
        return digest(reservation)

    async def begin(self,node,relation,request_id,reservation):
        _identifier(request_id)
        binding_sha=self._request(reservation)
        async with media_mutation_lock:
            physical=await joined_io(_capacity,self.root_provider)
            async with self.store.database.begin() as conn:
                current,active,binding=await self._owner(conn,node,relation)
                side=reservation['side'];own=current['node_id'];task_id=reservation['task_id']
                if (reservation[side+'_id']!=own or reservation['master_id']!=active['peer_id'] or
                    reservation[side+'_relationship']!=active['relationship_id']):
                    raise ReservationError('migration_reservation_member_mismatch')
                old=(await conn.execute(select(s.peer_reservations).where(
                    s.peer_reservations.c.task_id==task_id).with_for_update())).mappings().first()
                if old is not None:
                    if old['binding_sha256']!=binding_sha or old['payload']['owner_binding']!=binding:
                        raise ReservationError('migration_reservation_request_changed')
                    return self._status(await self._row(conn,node,relation,task_id),request_id)
                await fences.member_clear(conn,own)
                member=(await conn.execute(select(fs.storage_members).where(
                    fs.storage_members.c.member_id==own).with_for_update())).mappings().first()
                if not member or not member['storage_enabled'] or not member['writable']:
                    raise ReservationError('migration_reservation_storage_disabled')
                if member['reserved_bytes'] or await conn.scalar(select(fs.upload_sessions.c.upload_id).where(
                    fs.upload_sessions.c.state=='reserved').limit(1)):
                    raise ReservationError('migration_reservation_writer_pending')
                if await conn.scalar(text('SELECT COUNT(*) FROM media_delete_operations')):
                    raise ReservationError('migration_reservation_delete_pending')
                expected=reservation['total_bytes'] if side=='target' else 0
                available=min(member['allocated_bytes']-member['used_bytes']-member['reserved_bytes'],
                              physical-PHYSICAL_RESERVE_BYTES-member['reserved_bytes'])
                if side=='target' and available<expected:
                    raise ReservationError('migration_reservation_capacity_insufficient')
                now=int(self.wall())
                row=dict(task_id=task_id,master_id=active['peer_id'],member_id=own,relationship_id=active['relationship_id'],
                    side=side,state='preparing',binding_sha256=binding_sha,payload=dict(reservation=reservation,owner_binding=binding),
                    revision=1,expected_units=reservation['unit_count'],expected_objects=reservation['object_count'],expected_bytes=reservation['total_bytes'],
                    accepted_units=0,accepted_objects=0,accepted_bytes=0,last_unit_id='',units_sha256=INITIAL_UNITS_SHA256,created_at=now,updated_at=now)
                await conn.execute(insert(s.peer_reservations).values(**row))
                await conn.execute(insert(s.capacity).values(task_id=task_id,member_id=own,reserved_bytes=expected,updated_at=now))
                if side=='source':
                    await conn.execute(insert(s.drains).values(member_id=own,owner_id=task_id,reason='migration',created_at=now))
                else:
                    await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==own).values(
                        reserved_bytes=fs.storage_members.c.reserved_bytes+expected,updated_at=now))
                await self.store.log(conn,'migration_peer_reserved',active['peer_id'],task_id=task_id,side=side,reserved_bytes=expected)
                return self._status(row,request_id)

    def _unit(self,row,unit):
        if not isinstance(unit,dict) or unit.keys()!=UNIT_FIELDS:
            raise ReservationError('migration_reservation_unit_invalid')
        for name in ('unit_id','source_version'):_identifier(unit[name],hash_value=True)
        _positive(unit['object_count'],2**31-1);_positive(unit['total_bytes'])
        request=row['payload']['reservation'];kind=unit['resource_kind']
        for name in ('source_parent','target_parent'):
            path=unit[name]
            if not isinstance(path,str) or len(path)>1000:
                raise ReservationError('migration_reservation_unit_invalid')
            if kind=='media':
                validate_media_path(path+('/migration.mp3' if path.startswith('music/') else '/migration.mp4'))
            elif kind=='recording':
                parts=path.split('/')
                if len(parts)!=2 or any(not p.IDENTIFIER.fullmatch(value) for value in parts):
                    raise ReservationError('migration_reservation_unit_invalid')
            else:
                raise ReservationError('migration_reservation_unit_invalid')
        if (kind=='media' and unit['source_parent']!=unit['target_parent'] or
            kind=='recording' and (unit['source_parent'].split('/')[0]!=request['source_relationship'] or
                unit['target_parent'].split('/')[0]!=request['target_relationship'] or
                unit['source_parent'].split('/')[1]!=unit['target_parent'].split('/')[1]) or
            unit['unit_id']!=digest([kind,request['source_id'],unit['source_parent']])):
            raise ReservationError('migration_reservation_unit_changed')
        return unit['source_parent' if row['side']=='source' else 'target_parent']

    async def add_unit(self,node,relation,request_id,task_id,unit):
        _identifier(request_id)
        async with media_mutation_lock,self.store.database.begin() as conn:
            row=await self._row(conn,node,relation,task_id)
            path=self._unit(row,unit)
            old=(await conn.execute(select(s.units).where(s.units.c.task_id==task_id,
                s.units.c.unit_id==unit['unit_id']).with_for_update())).mappings().first()
            if old is not None:
                if any(old[name]!=value for name,value in dict(resource_kind=unit['resource_kind'],parent_path=path,
                    source_version=unit['source_version'],object_count=unit['object_count'],total_bytes=unit['total_bytes']).items()):
                    raise ReservationError('migration_reservation_unit_changed')
                if row['state'] in {'preparing','prepared'} and not await conn.scalar(select(s.scopes.c.scope_id).where(
                    s.scopes.c.task_id==task_id,s.scopes.c.member_id==row['member_id'],s.scopes.c.parent_path==path,
                    s.scopes.c.resource_kind==unit['resource_kind'])):
                    raise ReservationError('migration_reservation_scope_inconsistent')
                return self._status(row,request_id)
            if row['state']!='preparing' or unit['unit_id']<=row['last_unit_id']:
                raise ReservationError('migration_reservation_unit_order_invalid')
            totals=dict(accepted_units=row['accepted_units']+1,accepted_objects=row['accepted_objects']+unit['object_count'],
                        accepted_bytes=row['accepted_bytes']+unit['total_bytes'])
            if any(totals['accepted_'+part]>row['expected_'+part] for part in ('units','objects','bytes')):
                raise ReservationError('migration_reservation_totals_exceeded')
            await fences.paths(conn,unit['resource_kind'],[(path,True)],member_id=row['member_id'],owner_task_id=task_id)
            ancestors=['/'.join(path.split('/')[:index]) for index in range(1,len(path.split('/')))]
            if await conn.scalar(select(s.scopes.c.scope_id).where(s.scopes.c.member_id==row['member_id'],
                s.scopes.c.resource_kind==unit['resource_kind'],s.scopes.c.parent_path.in_(ancestors)).limit(1)):
                raise ReservationError('migration_reservation_parent_overlap')
            if await conn.scalar(select(fs.upload_sessions.c.upload_id).where(fs.upload_sessions.c.state=='reserved',
                fs.upload_sessions.c.media_path.startswith(path+'/',autoescape=True)).limit(1)):
                raise ReservationError('migration_reservation_writer_pending')
            if unit['resource_kind']=='recording':
                member_reserved=await conn.scalar(select(fs.storage_members.c.reserved_bytes).where(fs.storage_members.c.member_id==row['member_id']))
                media_reserved=int(await conn.scalar(select(func.coalesce(func.sum(fs.upload_sessions.c.expected_bytes),0))
                    .where(fs.upload_sessions.c.state=='reserved')) or 0)
                own_reserved=row['expected_bytes'] if row['side']=='target' else 0
                if member_reserved!=media_reserved+own_reserved:
                    raise ReservationError('migration_reservation_recording_writer_pending')
            now=int(self.wall())
            await conn.execute(insert(s.scopes).values(scope_id=digest([unit['resource_kind'],row['member_id'],path]),
                task_id=task_id,member_id=row['member_id'],resource_kind=unit['resource_kind'],parent_path=path,created_at=now))
            await conn.execute(insert(s.units).values(task_id=task_id,unit_id=unit['unit_id'],resource_kind=unit['resource_kind'],
                parent_path=path,source_version=unit['source_version'],state='pending',object_count=unit['object_count'],total_bytes=unit['total_bytes']))
            values=dict(**totals,last_unit_id=unit['unit_id'],units_sha256=unit_transcript(row['units_sha256'],unit),
                        revision=row['revision']+1,updated_at=now)
            await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==task_id).values(**values))
            await self.store.log(conn,'migration_peer_unit_reserved',row['master_id'],task_id=task_id,unit_id=unit['unit_id'])
            return self._status({**row,**values},request_id)

    async def seal(self,node,relation,request_id,task_id):
        _identifier(request_id)
        async with self.store.database.begin() as conn:
            row=await self._row(conn,node,relation,task_id)
            if (row['state'] not in {'preparing','prepared'} or any(row['accepted_'+part]!=row['expected_'+part] for part in ('units','objects','bytes')) or
                row['units_sha256']!=row['payload']['reservation']['units_sha256']):
                raise ReservationError('migration_reservation_incomplete')
            units=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
                func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==task_id,s.units.c.state=='pending'))).one()
            scopes=await conn.scalar(select(func.count()).select_from(s.scopes).where(s.scopes.c.task_id==task_id,
                                                                                   s.scopes.c.member_id==row['member_id']))
            if tuple(units)!=(row['expected_units'],row['expected_objects'],row['expected_bytes']) or scopes!=row['expected_units']:
                raise ReservationError('migration_reservation_scope_inconsistent')
            if row['state']=='prepared':return self._status(row,request_id)
            values=dict(state='prepared',revision=row['revision']+1,updated_at=int(self.wall()))
            await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==task_id).values(**values))
            await self.store.log(conn,'migration_peer_prepared',row['master_id'],task_id=task_id)
            return self._status({**row,**values},request_id)

    async def status(self,node,relation,request_id,task_id):
        _identifier(request_id)
        async with self.store.database.begin() as conn:
            return self._status(await self._row(conn,node,relation,task_id),request_id)

    async def abort_unstarted(self,node,relation,request_id,task_id):
        _identifier(request_id)
        async with media_mutation_lock,self.store.database.begin() as conn:
            row=await self._row(conn,node,relation,task_id)
            return await self._abort(conn,row,request_id)

    async def cancel_unstarted(self,node,relation,request_id,reservation):
        """Persist cancellation even when begin never arrived, fencing late begin."""
        _identifier(request_id)
        checksum=self._request(reservation)
        async with media_mutation_lock,self.store.database.begin() as conn:
            current,active,binding=await self._owner(conn,node,relation)
            side=reservation['side'];task_id=reservation['task_id'];own=current['node_id']
            if (reservation[side+'_id']!=own or reservation['master_id']!=active['peer_id'] or
                    reservation[side+'_relationship']!=active['relationship_id']):
                raise ReservationError('migration_reservation_member_mismatch')
            old=(await conn.execute(select(s.peer_reservations).where(s.peer_reservations.c.task_id==task_id))).mappings().first()
            if old is not None:
                if old['binding_sha256']!=checksum or old['payload']['owner_binding']!=binding:
                    raise ReservationError('migration_reservation_request_changed')
                return await self._abort(conn,await self._row(conn,node,relation,task_id),request_id)
            # Orphan authority cannot be hidden beneath an absence tombstone.
            for table in (s.units,s.capacity,s.scopes,s.objects,s.storage_journal,s.peer_bindings):
                if await conn.scalar(select(next(iter(table.primary_key.columns))).where(table.c.task_id==task_id).limit(1)):
                    raise ReservationError('migration_reservation_requires_reconciliation')
            if await conn.scalar(select(s.drains.c.owner_id).where(s.drains.c.owner_id==task_id)):
                raise ReservationError('migration_reservation_requires_reconciliation')
            now=int(self.wall())
            row=dict(task_id=task_id,master_id=active['peer_id'],member_id=own,relationship_id=active['relationship_id'],side=side,
                state='aborted',binding_sha256=checksum,payload=dict(reservation=reservation,owner_binding=binding),revision=1,
                expected_units=reservation['unit_count'],expected_objects=reservation['object_count'],expected_bytes=reservation['total_bytes'],
                accepted_units=0,accepted_objects=0,accepted_bytes=0,last_unit_id='',units_sha256=INITIAL_UNITS_SHA256,
                created_at=now,updated_at=now)
            await conn.execute(insert(s.peer_reservations).values(**row))
            await self.store.log(conn,'migration_peer_absence_cancelled',active['peer_id'],task_id=task_id,side=side)
            return self._status(row,request_id)

    async def _abort(self,conn,row,request_id):
        task_id=row['task_id']
        if row['state']=='aborted':return self._status(row,request_id)
        if row['state'] not in {'preparing','prepared'}:
            raise ReservationError('migration_reservation_requires_reconciliation')
        for table in (s.storage_journal,s.objects,s.peer_bindings):
            condition=table.c.task_id==task_id
            if await conn.scalar(select(next(iter(table.primary_key.columns))).where(condition).limit(1)):
                raise ReservationError('migration_reservation_requires_reconciliation')
        if await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==task_id,s.units.c.state!='pending').limit(1)):
            raise ReservationError('migration_reservation_requires_reconciliation')
        reserved=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==task_id).with_for_update())).mappings().one_or_none()
        expected=row['expected_bytes'] if row['side']=='target' else 0
        member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==row['member_id'])
                                  .with_for_update())).mappings().first()
        if (not reserved or reserved['member_id']!=row['member_id'] or reserved['reserved_bytes']!=expected or
            not member or member['reserved_bytes']<expected):
            raise ReservationError('migration_reservation_capacity_inconsistent')
        now=int(self.wall())
        if expected:
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==row['member_id']).values(
                reserved_bytes=fs.storage_members.c.reserved_bytes-expected,updated_at=now))
        for table in (s.scopes,s.capacity,s.units):
            await conn.execute(delete(table).where(table.c.task_id==task_id))
        await conn.execute(delete(s.drains).where(s.drains.c.member_id==row['member_id'],s.drains.c.owner_id==task_id,
                                                s.drains.c.reason=='migration'))
        values=dict(state='aborted',revision=row['revision']+1,updated_at=now)
        await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==task_id).values(**values))
        await self.store.log(conn,'migration_peer_reservation_aborted',row['master_id'],task_id=task_id)
        return self._status({**row,**values},request_id)
