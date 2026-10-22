"""Bounded immutable native migration history checks; never cleanup authority."""
import asyncio
import json
import stat
from types import SimpleNamespace

from sqlalchemy import String,Text,and_,cast,func,literal,select,tuple_

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.node_retirement_fences import RetirementError
from app.services.resource_commit_peers import CommitPeers
from app.services.resource_final_receipts import verify as final_receipt
from app.services.resource_observation import digest
from app.services.resource_peer_reservations import INITIAL_UNITS_SHA256,NativeReservations,unit_transcript
from app.services.resource_transfer_protocol import immutable_shape,target_encryption_digest

BATCH=16
JSON_LIMIT=65536


def fail(code='retirement_migration_history_invalid'):raise RetirementError(code)


def ident(value,*,long=False):
    if not isinstance(value,str) or not (p.OBJECT_ID if long else p.IDENTIFIER).fullmatch(value):fail()
    return value


def integer(value,*,positive=False):
    if type(value) is not int or not (0<value<2**53 if positive else 0<=value<2**53):fail()
    return value


def projection(table,fields,*,names=None):
    result=[]
    for column in table.c:
        name=column.name
        if names is not None and name not in names:continue
        limit=fields.get(name)
        if limit is None and isinstance(column.type,String):limit=column.type.length or JSON_LIMIT
        if limit is None:result.append(column);continue
        raw=cast(column,Text)
        result.extend((func.substr(raw,1,limit+1).label(name),func.length(raw).label('_length_'+name)))
    return result


def decoded(row,name,limit=JSON_LIMIT,*,canonical=False):
    raw=row[name]
    if (not isinstance(raw,str) or type(row['_length_'+name]) is not int or row['_length_'+name]>limit
            or len(raw.encode())>limit):fail('retirement_migration_metadata_limit')
    try:
        value=json.loads(raw);encoded=p.canonical(value)
        if not isinstance(value,dict) or len(encoded)>limit or canonical and encoded.decode()!=raw:fail()
        return value
    except (ValueError,TypeError,RecursionError):fail()


async def pages(conn,table,condition,*,fields=None,key=None):
    keys=(key,) if key is not None and not isinstance(key,tuple) else key or tuple(table.primary_key.columns)
    after=None
    while True:
        rows=list((await conn.execute(select(*projection(table,fields or {})).where(condition,
            tuple_(*keys)>after if after is not None else True).order_by(*keys).limit(BATCH))).mappings())
        if not rows:return
        for row in rows:
            value=dict(row)
            position=tuple(value[key.name] for key in keys)
            if after is not None and position<=after:fail()
            after=position;yield value
        await asyncio.sleep(0)


async def exists(conn,table,condition=True):
    return await conn.scalar(select(literal(1)).select_from(table).where(condition).limit(1)) is not None


def native_shape(value,size):
    if (not isinstance(value,dict) or value.keys()!={'bytes','device','inode','mtime_ns','ctime_ns','mode','links'} or
            any(type(item) is not int or not 0<=item<2**63 for item in value.values()) or
            value['bytes']!=size or not value['inode'] or not value['links'] or not stat.S_ISREG(value['mode'])):fail()
    return value


async def terminal_clear(conn,task_id,now):
    for table,condition in ((s.capacity,s.capacity.c.task_id==task_id),(s.scopes,s.scopes.c.task_id==task_id),
            (s.drains,s.drains.c.owner_id==task_id),
            (s.runners,(s.runners.c.task_id==task_id)&(s.runners.c.expires_at>now)),
            (s.read_leases,(s.read_leases.c.task_id==task_id)&(s.read_leases.c.expires_at>now))):
        if await exists(conn,table,condition):fail('retirement_migration_work_remaining')


async def orphans(conn):
    # Preview headers are legal without a task; admitted headers are not.
    for table,parent,left,right in ((s.plan_bindings,s.plans,'plan_id','plan_id'),(s.plan_paths,s.plans,'plan_id','plan_id'),
            (s.objects,s.tasks,'task_id','task_id'),(s.peer_bindings,s.peer_reservations,'task_id','task_id'),
            (s.storage_journal,s.peer_reservations,'task_id','task_id'),(s.runners,s.tasks,'task_id','task_id')):
        if await conn.scalar(select(literal(1)).select_from(table.outerjoin(parent,table.c[left]==parent.c[right]))
                .where(parent.c[right].is_(None)).limit(1)) is not None:fail('retirement_migration_orphan')
    unit_owner=s.units.outerjoin(s.tasks,s.units.c.task_id==s.tasks.c.task_id).outerjoin(s.peer_reservations,s.units.c.task_id==s.peer_reservations.c.task_id)
    if await conn.scalar(select(literal(1)).select_from(unit_owner).where(
            and_(s.tasks.c.task_id.is_(None),s.peer_reservations.c.task_id.is_(None))|
            and_(s.tasks.c.task_id.is_not(None),s.peer_reservations.c.task_id.is_not(None))).limit(1)) is not None:fail('retirement_migration_orphan')
    plans=s.plans.outerjoin(s.tasks,s.plans.c.plan_id==s.tasks.c.task_id)
    if await conn.scalar(select(literal(1)).select_from(plans).where(
            and_(s.plans.c.state=='admitted',s.tasks.c.task_id.is_(None))|
            and_(s.plans.c.state!='admitted',s.tasks.c.task_id.is_not(None))).limit(1)) is not None:fail('retirement_migration_orphan')


class NativeHistory:
    def __init__(self,store,now):self.store,self.now=store,now;self.reservations=NativeReservations(store)

    async def _header(self,conn,node,row):
        for name in ('task_id','master_id','member_id','relationship_id'):ident(row[name])
        payload=decoded(row,'payload')
        if set(payload)-{'reservation','owner_binding','transfer','final_receipt','source_final_receipt','cancel_release','cancel_target_release'}:fail()
        request=payload.get('reservation')
        try:checksum=self.reservations._request(request)
        except (p.ProtocolError,TypeError,KeyError):fail()
        if row['binding_sha256']!=checksum or row['state'] not in {'aborted','released'} or row['side'] not in {'source','target'}:fail()
        if (request['task_id']!=row['task_id'] or request['master_id']!=row['master_id'] or request['side']!=row['side']
                or request[row['side']+'_id']!=row['member_id'] or request[row['side']+'_relationship']!=row['relationship_id']):fail()
        for name in ('units','objects','bytes'):
            if row['expected_'+name]!=request[{'units':'unit_count','objects':'object_count','bytes':'total_bytes'}[name]]:fail()
            if not 0<=integer(row['accepted_'+name])<=row['expected_'+name]:fail()
        integer(row['revision'],positive=True);integer(row['created_at'],positive=True);integer(row['updated_at'],positive=True)
        if not row['created_at']<=row['updated_at']<=self.now+5:fail()
        ident(payload.get('owner_binding'),long=True);ident(row['units_sha256'],long=True)
        if row['accepted_units']==0:
            if row['last_unit_id'] or row['units_sha256']!=INITIAL_UNITS_SHA256:fail()
        else:ident(row['last_unit_id'],long=True)
        relation=(await conn.execute(select(*projection(fs.relationships,{},names={'relationship_id','peer_id','direction','peer_key',
            'credential','peer_endpoint','mode','protocol'})).where(fs.relationships.c.relationship_id==row['relationship_id']))).mappings().first()
        if not relation or relation['peer_id']!=row['master_id'] or relation['direction']!='upstream':fail()
        if row['member_id']==node['node_id']:
            owners=[digest([node['node_id'],node['endpoint'],relation['relationship_id'],relation['peer_id'],relation['peer_key'],
                relation['credential'],relation['peer_endpoint'],mode,relation['protocol']]) for mode in ('Direct','Relay')]
            if payload['owner_binding'] not in owners:fail('retirement_migration_owner_changed')
        control=payload.get('transfer',{})
        if not isinstance(control,dict):fail()
        if control:
            integer(control.get('epoch'),positive=True);ident(control.get('runner_id'));integer(control.get('expires_at'),positive=True)
            if control.get('phase') not in {'probe','copy','abort','publish','verify','commit','inspect','cleanup','finalize'}:fail()
        return payload,request,control

    def _binding(self,node,header,request,control,row):
        value=decoded(row,'binding_json',16384,canonical=True)
        try:immutable_shape(value)
        except (p.ProtocolError,TypeError,KeyError):fail()
        obj=value['object'];side=header['side']
        if (digest(value)!=row['binding_sha256'] or obj['logical_id']!=row['logical_id'] or obj['unit_id']!=row['unit_id']
                or obj['source_path']!=row['source_path'] or any(value[name]!=request[name] for name in
                    ('task_id','master_id','source_id','target_id','source_relationship','target_relationship','plan_sha256','units_sha256'))):fail()
        for which in ('source','target'):
            expected=digest({**request,'side':which})
            if value[which+'_binding']!=expected:fail()
        if header['member_id']==node['node_id'] and (value[side+'_key']!=p.public_key(self.store.unseal(node['private_key'])) or value[side+'_endpoint']!=node['endpoint']):fail()
        common={name:item for name,item in value.items() if name!='object'}
        if control.get('probe_verified') is not True or control.get('probe_binding')!=digest(common):fail()
        return value,common

    async def _objects(self,conn,node,header,request,control):
        first=None;count=total=bindings=binding_bytes=0
        async for binding in pages(conn,s.peer_bindings,s.peer_bindings.c.task_id==header['task_id'],fields={'binding_json':16384},key=s.peer_bindings.c.logical_id):
            value,common=self._binding(node,header,request,control,binding);obj=value['object']
            bindings+=1;binding_bytes+=obj['expected_bytes']
            if bindings>header['expected_objects'] or binding_bytes>header['expected_bytes']:fail()
            if first is None:first=common
            elif first!=common:fail()
            row=(await conn.execute(select(*projection(s.storage_journal,{'publication':JSON_LIMIT})).where(
                s.storage_journal.c.task_id==header['task_id'],s.storage_journal.c.logical_id==binding['logical_id']))).mappings().first()
            if row is None:
                if header['state']=='released':fail()
                continue
            journal=dict(row);publication=decoded(journal,'publication')
            if (any(journal[name]!=value[name] for name in ('task_id','master_id','source_id','target_id')) or
                    journal['unit_id']!=obj['unit_id'] or journal['object_id']!=obj['source_object'] or journal['resource_kind']!=obj['resource_kind']
                    or journal['physical_path']!=obj[header['side']+'_path'] or journal['source_version']!=obj['source_version']
                    or journal['expected_bytes']!=obj['expected_bytes']):fail()
            if header['state']=='released':
                if journal['state']!=('deleted' if header['side']=='source' else 'published') or journal['sha256']!=obj['sha256']:fail()
                unit=(await conn.execute(select(*projection(s.units,{'publication':JSON_LIMIT})).where(
                    s.units.c.task_id==header['task_id'],s.units.c.unit_id==obj['unit_id']))).mappings().first()
                if not unit or unit['source_version']!=obj['unit_version'] or unit['parent_path']!=obj[header['side']+'_path'].rsplit('/',1)[0]:fail()
                unit_publication=decoded(unit,'publication')
                if header['side']=='target':
                    if journal['publication_phase']!=3 or set(publication)!={'verified_native','assembled_native','encryption'}:fail()
                    native_shape(publication['verified_native'],obj['expected_bytes']);native_shape(publication['assembled_native'],obj['expected_bytes'])
                    if digest(publication['encryption'])!=target_encryption_digest(obj):fail()
                elif (journal['stage_identity'] or publication.keys()!={'action','native','commit_sha256'} or
                        publication['action']!='source-cleanup' or publication['commit_sha256']!=unit_publication.get('commit_sha256') or
                        journal['cleanup_version']!=digest(native_shape(publication['native'],obj['expected_bytes']))):fail()
            else:
                if journal['state']!='aborted' or journal['publication_phase'] or set(publication)-{'verified_native'}:fail()
                if journal['sha256']:
                    if journal['sha256']!=obj['sha256'] or 'verified_native' not in publication:fail()
                elif publication:fail()
                if publication:
                    observed=native_shape(publication['verified_native'],obj['expected_bytes'])
                    if journal['stage_identity']!=digest([observed['device'],observed['inode']]):fail()
                for name in ('stage_identity','cleanup_version'):
                    if journal[name]:ident(journal[name],long=True)
            count+=1;total+=obj['expected_bytes'];integer(total)
        if await exists(conn,s.storage_journal,(s.storage_journal.c.task_id==header['task_id'])&~select(s.peer_bindings.c.logical_id).where(
                s.peer_bindings.c.task_id==s.storage_journal.c.task_id,s.peer_bindings.c.logical_id==s.storage_journal.c.logical_id).exists()):fail('retirement_migration_orphan')
        return first,count,total

    async def _units(self,conn,header,common):
        request=header['_request'];count=objects=total=0;transcript=INITIAL_UNITS_SHA256;last=''
        checker=CommitPeers(SimpleNamespace(store=self.store,wall=lambda:self.now))
        async for unit in pages(conn,s.units,s.units.c.task_id==header['task_id'],fields={'publication':JSON_LIMIT},key=s.units.c.unit_id):
            if header['state']!='released':fail()
            publication=decoded(unit,'publication');kind=unit['resource_kind'];parent=unit['parent_path'];side=header['side']
            source_parent=parent if side=='source' or kind=='media' else request['source_relationship']+'/'+parent.rsplit('/',1)[-1]
            target_parent=parent if side=='target' or kind=='media' else request['target_relationship']+'/'+parent.rsplit('/',1)[-1]
            description=dict(unit_id=unit['unit_id'],resource_kind=kind,source_parent=source_parent,target_parent=target_parent,
                source_version=unit['source_version'],object_count=unit['object_count'],total_bytes=unit['total_bytes'])
            try:self.reservations._unit(header,description)
            except (p.ProtocolError,TypeError,KeyError):fail()
            if (unit['state']!=('cleaned' if side=='source' else 'committed') or not 0<unit['committed_at']<unit['cleanup_after']<=self.now
                    or unit['cleanup_after']-unit['committed_at']!=3600):fail()
            commit=publication.get('commit');grant={**common,'epoch':header['_control']['epoch']}
            try:checksum=checker._record(commit,grant)
            except (p.ProtocolError,TypeError,KeyError):fail()
            if (checksum!=publication.get('commit_sha256') or commit['unit_id']!=unit['unit_id']
                    or any(commit[name]!=description[other] for name,other in (('resource_kind','resource_kind'),('source_parent','source_parent'),('target_parent','target_parent'),
                        ('unit_version','source_version'),('object_count','object_count'),('total_bytes','total_bytes')))
                    or commit['committed_at']!=unit['committed_at'] or commit['cleanup_after']!=unit['cleanup_after']):fail()
            if side=='source' and digest(publication.get('source_verification'))!=commit['source_receipt_sha256']:fail()
            if side=='target' and publication.get('receipt_sha256')!=commit['target_receipt_sha256']:fail()
            roster=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.storage_journal.c.expected_bytes),0)).where(
                s.storage_journal.c.task_id==header['task_id'],s.storage_journal.c.unit_id==unit['unit_id']))).one()
            if tuple(roster)!=(unit['object_count'],unit['total_bytes']):fail()
            transcript=unit_transcript(transcript,description);count+=1;objects+=unit['object_count'];total+=unit['total_bytes'];last=unit['unit_id']
        if header['state']=='released' and (count,objects,total,transcript,last)!=(header['expected_units'],header['expected_objects'],header['expected_bytes'],header['units_sha256'],header['last_unit_id']):fail()

    async def _verify(self,conn,node):
        await orphans(conn);checked=0
        async for row in pages(conn,s.peer_reservations,True,fields={'payload':JSON_LIMIT}):
            payload,request,control=await self._header(conn,node,row)
            header={**row,'payload':payload,'_request':request,'_control':control}
            await terminal_clear(conn,row['task_id'],self.now)
            if await exists(conn,s.objects,s.objects.c.task_id==row['task_id']):fail()
            if 'cancel_release' in payload or 'cancel_target_release' in payload:
                from app.services.node_retirement_cancel_history import CancellationHistory
                await CancellationHistory(self).verify(conn,node,header,payload,request,control)
                checked+=1;continue
            common,count,total=await self._objects(conn,node,header,request,control)
            if row['state']=='released' and common is None:fail()
            await self._units(conn,header,common)
            if row['state']=='released':
                if common is None or (count,total)!=(row['expected_objects'],row['expected_bytes']) or any(row['accepted_'+name]!=row['expected_'+name] for name in ('units','objects','bytes')):fail()
                counts=row['expected_units'],row['expected_objects'],row['expected_bytes'];grant={**common,'epoch':control['epoch']}
                if control.get('phase')!='finalize' or control.get('commit_started') is not True:fail()
                try:
                    if row['side']=='target':final_receipt(payload.get('source_final_receipt'),grant,'source',counts,self.now)
                    receipt=final_receipt(payload.get('final_receipt'),grant,row['side'],counts,self.now,source_receipt=payload.get('source_final_receipt'))
                except (p.ProtocolError,TypeError,KeyError):fail('retirement_migration_receipt_invalid')
                if not row['created_at']<=receipt['finished_at']<=row['updated_at'] or receipt['binding_sha256']!=row['binding_sha256']:fail()
            elif (payload.get('final_receipt') is not None or payload.get('source_final_receipt') is not None or control and
                    (control.get('phase')!='abort' or control.get('stop_complete') is not True or
                        any(row['accepted_'+name]!=row['expected_'+name] for name in ('units','objects','bytes')))):
                fail()
            checked+=1
        return dict(native_tasks=checked)

    async def verify(self,conn,node):
        try:return await self._verify(conn,node)
        except RetirementError:raise
        except (ValueError,TypeError,KeyError,OverflowError,RecursionError):fail()
