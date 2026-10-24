"""Native direct range transfer into private staging, without publication."""
import asyncio
import hashlib
import json
import os
import ssl
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from anyio import CancelScope
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.core.joined_io import joined_io
from app.services import resource_migration_schema as s, resource_transfer_files as files
from app.services.federation import protocol as p, schema as fs
from app.services.resource_inventory import roots
from app.services.resource_observation import ContentObserver, digest, native
from app.services.resource_peer_reservations import NativeReservations
from app.services.resource_transfer_protocol import (CHUNK_BYTES,CONTROL_BYTES,SOURCE_PATH,TransferError,
    grant_shape,immutable,target_request,verify_target)
from app.services.resource_pool import PHYSICAL_RESERVE_BYTES


class TransferPeers:
    def __init__(self,store,*,root_provider=roots,traffic=None,wall=time.time,client=None):
        from app.services.business_traffic import monitor
        self.store,self.root_provider,self.wall=store,root_provider,wall
        self.traffic=monitor if traffic is None else traffic
        self.native=NativeReservations(store,root_provider=root_provider,wall=wall)
        self.observer=ContentObserver(store,root_provider=root_provider,traffic=self.traffic,wall=wall)
        self.client=client;self.owns_client=client is None
        self.target_lock=asyncio.Lock();self.source_lock=asyncio.Lock()
        self.epochs={};self.hash_state=None
        self.publication_scan=None
        self.publication_roster=None
        self.source_verification=None
        self.target_verification=None
        self.source_cleanup_scan=None
        self.source_finalization_scan=None
        self.source_cancel_census=None

    async def close(self):
        if self.source_cancel_census is not None:
            await joined_io(self.source_cancel_census.close);self.source_cancel_census=None
        if self.source_finalization_scan is not None:
            await joined_io(self.source_finalization_scan.close);self.source_finalization_scan=None
        if self.source_cleanup_scan is not None:
            await joined_io(self.source_cleanup_scan.close);self.source_cleanup_scan=None
        if self.target_verification is not None:
            await joined_io(self.target_verification.close);self.target_verification=None
        if self.source_verification is not None:
            await joined_io(self.source_verification.close);self.source_verification=None
        if self.publication_scan is not None:
            await joined_io(self.publication_scan.close);self.publication_scan=None
        if self.client is not None and self.owns_client:await self.client.aclose();self.client=None
        await self.observer.stop()

    async def reap_cancel_census(self):
        while True:
            await asyncio.sleep(1)
            scan=self.source_cancel_census
            if scan is not None and self.wall()>=scan.deadline:
                async with self.source_lock:
                    if self.source_cancel_census is scan and self.wall()>=scan.deadline:
                        await joined_io(scan.close);self.source_cancel_census=None

    def _client(self):
        if self.client is None:
            self.client=httpx.AsyncClient(verify=ssl.create_default_context(),trust_env=False,follow_redirects=False,
                timeout=httpx.Timeout(50,connect=8),limits=httpx.Limits(max_connections=1,max_keepalive_connections=1),
                headers={'Accept-Encoding':'identity'})
        return self.client

    @asynccontextmanager
    async def _remote(self,grant,request):
        from app.services.resource_control_errors import LIMIT,verify
        raw=p.canonical(request)
        try:
            async with asyncio.timeout(max(0,grant['expires_at']-self.wall())):
                async with self._client().stream('POST',grant['source_endpoint']+SOURCE_PATH,content=raw,
                        headers={'Content-Type':'application/json'}) as result:
                    if result.status_code!=200:
                        body=bytearray()
                        async for chunk in result.aiter_bytes():
                            if len(body)+len(chunk)>LIMIT:raise TransferError('migration_transfer_source_error_invalid')
                            body.extend(chunk)
                        try:
                            code=verify(json.loads(body),key=grant['source_key'],node_id=grant['source_id'],master_id=grant['master_id'],
                                relationship_id=grant['source_relationship'],path=SOURCE_PATH,raw=raw,status=result.status_code)
                        except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError):
                            code='migration_transfer_source_http_'+str(result.status_code)
                        raise TransferError(code)
                    yield result
        except (httpx.TimeoutException,TimeoutError):raise TransferError('migration_transfer_direct_timeout') from None
        except httpx.ConnectError as exc:
            code='migration_transfer_tls_verification_failed' if 'CERTIFICATE_VERIFY_FAILED' in str(exc) else 'migration_transfer_direct_unavailable'
            raise TransferError(code) from None
        except httpx.HTTPError:raise TransferError('migration_transfer_direct_unavailable') from None

    async def _bind(self,envelope,side,*,expected=None,target_value=None,authenticated_relation=None):
        if len(p.canonical(envelope))>CONTROL_BYTES or not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:
            raise TransferError('migration_transfer_grant_invalid')
        raw=envelope['payload']
        if not isinstance(raw,dict):raise TransferError('migration_transfer_grant_invalid')
        relationship=raw.get(side+'_relationship')
        from app.services.resource_transfer_protocol import identifier
        identifier(relationship)
        async with self.store.database.begin() as conn:
            node=await self.store.lock(conn)
            relation=(await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==relationship))).mappings().first()
            if not relation:raise TransferError('migration_transfer_master_invalid')
            grant=grant_shape(p.verify(relation['peer_key'],envelope),self.wall())
            proof=verify_target(target_value,grant,self.wall()) if target_value is not None else None
            if authenticated_relation is not None and any(relation[name]!=authenticated_relation.get(name) for name in
                    ('relationship_id','peer_id','peer_key','credential','direction','state','protocol','mode')):
                raise TransferError('migration_transfer_master_changed')
            if expected and grant['purpose']!=expected:raise TransferError('migration_transfer_purpose_invalid')
            row=await self.native._row(conn,node,relation,grant['task_id'])
            request=row['payload']['reservation']
            if (row['side']!=side or row['master_id']!=grant['master_id'] or row['binding_sha256']!=grant[side+'_binding'] or
                    node['node_id']!=grant[side+'_id'] or node['endpoint']!=grant[side+'_endpoint'] or
                    p.public_key(self.store.unseal(node['private_key']))!=grant[side+'_key'] or
                    any(request[name]!=grant[name] for name in ('source_id','target_id','source_relationship','target_relationship','plan_sha256','units_sha256')) or
                    row['state'] not in ({'prepared','active','aborting','aborted'} if grant['purpose']=='abort' else
                        {'active','released'} if grant['purpose']=='finalize' else {'prepared','active'})):
                raise TransferError('migration_transfer_reservation_changed')
            if row['state']=='aborted':return grant,dict(relation)
            if row['state']=='released':
                if grant['epoch']<row['payload'].get('transfer',{}).get('epoch',0):
                    raise TransferError('migration_transfer_epoch_stale')
                self.epochs={grant['task_id']:(grant['epoch'],grant['runner_id'],grant['purpose'])}
                return grant,dict(relation)
            if grant['purpose']=='abort':
                for table,condition in ((s.units,s.units.c.state.not_in(['pending','copying','verified','cancelled'])),
                    (s.storage_journal,s.storage_journal.c.state.not_in(['receiving','verified','aborting','aborted']))):
                    if await conn.scalar(select(next(iter(table.primary_key.columns))).where(table.c.task_id==grant['task_id'],condition).limit(1)):
                        raise TransferError('migration_abort_published_material')
            if proof is not None:
                expired=list((await conn.execute(select(fs.requests.c.nonce).where(fs.requests.c.relationship_id==relation['relationship_id'],
                    fs.requests.c.expires_at<=int(self.wall())).limit(32))).scalars())
                if expired:await conn.execute(delete(fs.requests).where(fs.requests.c.relationship_id==relation['relationship_id'],fs.requests.c.nonce.in_(expired)))
                expired_readers=list((await conn.execute(select(s.read_leases.c.lease_id).where(
                    s.read_leases.c.member_id==node['node_id'],s.read_leases.c.expires_at<=int(self.wall())).limit(32))).scalars())
                if expired_readers:await conn.execute(delete(s.read_leases).where(s.read_leases.c.member_id==node['node_id'],
                    s.read_leases.c.lease_id.in_(expired_readers)))
                try:await conn.execute(insert(fs.requests).values(relationship_id=relation['relationship_id'],nonce=proof['nonce'],expires_at=grant['expires_at']+5))
                except IntegrityError as exc:raise TransferError('migration_transfer_replayed') from exc
            task_binding=digest({name:value for name,value in immutable(grant).items() if name!='object'})
            control=dict(row['payload'].get('transfer',{}))
            if grant['purpose']=='publish' and not control.get('publication_started'):
                from sqlalchemy import func
                count=await conn.scalar(select(func.count()).select_from(s.peer_bindings).where(s.peer_bindings.c.task_id==grant['task_id']))
                if count!=row['expected_objects']:raise TransferError('migration_publish_metadata_incomplete')
            if control.get('publication_started') and grant['purpose'] not in {'copy','publish','verify','cancel','commit','inspect','cleanup','finalize'}:
                raise TransferError('migration_transfer_publication_started')
            if control.get('cancel_started') and grant['purpose'] in {'register','copy','probe','publish','abort'}:
                raise TransferError('migration_transfer_cancellation_started')
            if control.get('commit_started') and grant['purpose'] in {'register','probe','abort'}:
                raise TransferError('migration_transfer_commit_started')
            old_epoch=control.get('epoch',0)
            if (grant['epoch']<old_epoch or grant['epoch']==old_epoch and
                    (control.get('runner_id')!=grant['runner_id'] or control.get('phase')=='pause')):
                raise TransferError('migration_transfer_epoch_stale')
            if grant['purpose'] in {'register','copy','cancel','publish','verify','commit','inspect','cleanup','finalize'} and (not control.get('probe_verified') or control.get('probe_binding')!=task_binding):
                raise TransferError('migration_transfer_probe_required')
            if grant['purpose']=='probe' and row['state']!='prepared':
                raise TransferError('migration_transfer_already_started')
            if grant['purpose'] in {'copy','register'}:
                obj=grant['object']
                unit=(await conn.execute(select(s.units).where(s.units.c.task_id==grant['task_id'],s.units.c.unit_id==obj['unit_id']))).mappings().first()
                if (not unit or unit['resource_kind']!=obj['resource_kind'] or unit['source_version']!=obj['unit_version'] or
                        unit['parent_path']!=obj[side+'_path'].rsplit('/',1)[0] or unit['state'] not in {'pending','copying','verified'}):
                    raise TransferError('migration_transfer_unit_changed')
                if side=='source' and unit['publication'].get('source_verification'):
                    raise TransferError('migration_transfer_unit_verified')
                binding=immutable(grant);checksum=digest(binding)
                old=(await conn.execute(select(s.peer_bindings).where(s.peer_bindings.c.task_id==grant['task_id'],
                    s.peer_bindings.c.logical_id==obj['logical_id']))).mappings().first()
                if old and old['binding_sha256']!=checksum:raise TransferError('migration_transfer_object_changed')
                if not old:
                    from sqlalchemy import func
                    count=control.get('metadata_objects')
                    unit_count=unit['publication'].get('metadata_objects')
                    # Reconcile an older journal once; never recount an
                    # increasing roster on every object registration/copy.
                    if count is None:count=await conn.scalar(select(func.count()).select_from(s.peer_bindings).where(s.peer_bindings.c.task_id==grant['task_id']))
                    if unit_count is None:unit_count=await conn.scalar(select(func.count()).select_from(s.peer_bindings).where(s.peer_bindings.c.task_id==grant['task_id'],s.peer_bindings.c.unit_id==obj['unit_id']))
                    if (type(count) is not int or type(unit_count) is not int or not 0<=count<row['expected_objects'] or
                            not 0<=unit_count<unit['object_count']):
                        raise TransferError('migration_transfer_object_roster_exceeded')
                    await conn.execute(insert(s.peer_bindings).values(task_id=grant['task_id'],logical_id=obj['logical_id'],
                        unit_id=obj['unit_id'],source_path=obj['source_path'],binding_sha256=checksum,binding_json=p.canonical(binding).decode()))
                    control['metadata_objects']=count+1
                    await conn.execute(update(s.units).where(s.units.c.task_id==grant['task_id'],s.units.c.unit_id==obj['unit_id'])
                        .values(publication={**unit['publication'],'metadata_objects':unit_count+1}))
                if grant['purpose']=='copy':
                    await conn.execute(update(s.units).where(s.units.c.task_id==grant['task_id'],s.units.c.unit_id==obj['unit_id']).values(state='copying'))
            expires=max(control.get('expires_at',0),grant['expires_at']) if grant['epoch']==old_epoch else grant['expires_at']
            control.update(epoch=grant['epoch'],runner_id=grant['runner_id'],phase=grant['purpose'],expires_at=expires)
            if grant['purpose']=='publish':control['publication_started']=True
            if grant['purpose']=='cancel':control['cancel_started']=True
            payload={**row['payload'],'transfer':control}
            await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==grant['task_id']).values(
                payload=payload,state=('active' if grant['purpose'] in {'copy','publish'} else 'aborting' if grant['purpose']=='abort' else row['state']),
                updated_at=int(self.wall())))
        self.epochs={grant['task_id']:(grant['epoch'],grant['runner_id'],grant['purpose'])}
        return grant,dict(relation)

    async def register(self,node,relation,request_id,grant):
        """Record signed metadata only; never open a resource or create a part."""
        from app.services.resource_transfer_protocol import identifier
        identifier(request_id)
        raw=grant.get('payload') if isinstance(grant,dict) else None
        if not isinstance(raw,dict):raise TransferError('migration_transfer_grant_invalid')
        side='source' if node['node_id']==raw.get('source_id') else 'target'
        lock=self.source_lock if side=='source' else self.target_lock
        if lock.locked():raise TransferError('migration_transfer_'+side+'_busy')
        async with lock:
            value,_=await self._bind(grant,side,expected='register',authenticated_relation=relation)
            return dict(format='frontiercloud-python-transfer-metadata',version=1,request_id=request_id,
                node_id=node['node_id'],task_id=value['task_id'],logical_id=value['object']['logical_id'],
                epoch=value['epoch'],grant_sha256=digest(grant),registered=True,expires_at=value['expires_at'])

    def _alive(self,grant):
        if self.wall()>=grant['expires_at'] or self.epochs.get(grant['task_id'])!=(grant['epoch'],grant['runner_id'],grant['purpose']):
            raise TransferError('migration_transfer_window_expired')
        if grant['purpose'] not in {'abort','cancel','commit'} and not self.traffic.snapshot()['can_run']:raise TransferError('migration_transfer_waiting_low_traffic')

    async def _confirm(self,grant):
        self._alive(grant)
        async with self.store.database.begin() as conn:
            return await self._control_row(conn,grant)

    async def _control_row(self,conn,grant):
        self._alive(grant)
        node=await self.store.lock(conn)
        side='source' if node['node_id']==grant['source_id'] else 'target'
        relation=(await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==grant[side+'_relationship']))).mappings().one()
        row=await self.native._row(conn,node,relation,grant['task_id'])
        control=row['payload'].get('transfer',{})
        if (control.get('epoch'),control.get('runner_id'),control.get('phase'))!=(grant['epoch'],grant['runner_id'],grant['purpose']):
            raise TransferError('migration_transfer_epoch_stale')
        return row

    async def _probed(self,grant,*,staging_roots=None,source_roots=None):
        async with self.store.database.begin() as conn:
            row=await self._control_row(conn,grant)
            control={**row['payload']['transfer'],'probe_verified':True,
                'probe_binding':digest({name:value for name,value in immutable(grant).items() if name!='object'})}
            if staging_roots is not None:control['staging_roots']=staging_roots
            if source_roots is not None:control['source_roots']=source_roots
            await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==grant['task_id']).values(
                payload={**row['payload'],'transfer':control}))

    def source_reply(self,envelope,grant,proof):
        value=dict(format='frontiercloud-python-transfer-source',version=1,node_id=grant['source_id'],target_id=grant['target_id'],
            grant_sha256=digest(envelope),request_nonce=proof['nonce'],offset=proof['offset'],length=proof['length'],
            object_sha256=grant['object']['sha256'] if grant['object'] else '')
        return p.sign(self.store.unseal(self.store.node['private_key']),value)

    def _verify_source(self,envelope,request,grant):
        try:value=p.verify(grant['source_key'],envelope)
        except p.ProtocolError as exc:raise TransferError('migration_transfer_source_proof_invalid') from exc
        expected=dict(format='frontiercloud-python-transfer-source',version=1,node_id=grant['source_id'],target_id=grant['target_id'],
            grant_sha256=digest(request['grant']),request_nonce=request['proof']['payload']['nonce'],
            offset=request['proof']['payload']['offset'],length=request['proof']['payload']['length'],
            object_sha256=grant['object']['sha256'] if grant['object'] else '')
        if type(value.get('version')) is not int or value!=expected:raise TransferError('migration_transfer_source_proof_invalid')

    async def source(self,value):
        if not isinstance(value,dict) or value.keys()!={'grant','proof'}:raise TransferError('migration_transfer_request_invalid')
        if value.get('grant',{}).get('payload',{}).get('purpose') not in {'probe','copy'}:
            raise TransferError('migration_transfer_purpose_invalid')
        grant,relation=await self._bind(value['grant'],'source',target_value=value)
        proof=verify_target(value,grant,self.wall())
        self._alive(grant)
        if grant['purpose']=='probe':
            await self.native.seal(self.store.node,relation,proof['nonce'],grant['task_id'])
            from app.services.resource_final_roots import capture
            source_roots=await joined_io(capture,self.root_provider())
            await self._probed(grant,source_roots=source_roots)
            return self.source_reply(value['grant'],grant,proof),None
        if grant['purpose']!='copy':raise TransferError('migration_transfer_purpose_invalid')
        if self.source_lock.locked():raise TransferError('migration_transfer_source_busy')
        await self.source_lock.acquire()
        descriptor=None;lease=False
        try:
            obj=grant['object']
            async with self.store.database.begin() as conn:
                await self.store.lock(conn)
                await conn.execute(insert(s.read_leases).values(lease_id=proof['nonce'],member_id=grant['source_id'],logical_id=obj['logical_id'],
                    object_id=obj['source_object'],source_version=obj['source_version'],expires_at=grant['expires_at']+5,
                    task_id=grant['task_id'],purpose='migration'))
            lease=True
            header=dict(node_id=grant['source_id'],master_id=grant['master_id'],relationship_id=grant['source_relationship'],
                resource_kind=obj['resource_kind'],object_id=obj['source_object'],user_id=obj['user_id'])
            record=SimpleNamespace(header=header,relation=relation)
            descriptor,identity=await self.observer._open(record)
            expected=dict(physical_path=obj['source_path'],bytes=obj['expected_bytes'],source_version=obj['source_version'],
                metadata_sha256=obj['metadata_sha256'],encryption_sha256=obj['encryption_sha256'])
            if any(identity[name]!=value for name,value in expected.items()):raise TransferError('migration_transfer_source_changed')
            closed=False
            async def close():
                nonlocal closed
                if not closed:
                    closed=True;await self._source_close(descriptor,proof['nonce'])
            async def stream():
                try:
                    offset=proof['offset'];end=offset+proof['length']
                    while offset<end:
                        self._alive(grant)
                        data=await joined_io(os.pread,descriptor,min(CHUNK_BYTES,end-offset),offset)
                        self._alive(grant)
                        if not data:raise TransferError('migration_transfer_source_truncated')
                        offset+=len(data);yield data
                    if native(os.fstat(descriptor))!=identity['native']:raise TransferError('migration_transfer_source_changed')
                    await self._confirm(grant)
                finally:await close()
            return self.source_reply(value['grant'],grant,proof),SimpleNamespace(body=stream(),close=close)
        except BaseException:
            if descriptor is not None:os.close(descriptor)
            if lease:
                async with self.store.database.begin() as conn:await conn.execute(delete(s.read_leases).where(s.read_leases.c.lease_id==proof['nonce']))
            self.source_lock.release();raise

    async def _source_close(self,descriptor,lease_id):
        os.close(descriptor)
        try:
            # StreamingResponse disconnect cancellation is level-triggered.
            # Complete the short DB close before releasing its reader lock.
            with CancelScope(shield=True):
                async with self.store.database.begin() as conn:
                    await conn.execute(delete(s.read_leases).where(s.read_leases.c.lease_id==lease_id))
        finally:self.source_lock.release()

    def _reply(self,request_id,envelope,grant,*,state,received=0,hashed=0,sha256=''):
        return dict(format='frontiercloud-python-target-transfer',version=1,request_id=request_id,task_id=grant['task_id'],
            node_id=grant['target_id'],epoch=grant['epoch'],grant_sha256=digest(envelope),state=state,
            logical_id=grant['object']['logical_id'] if grant['object'] else '',received_bytes=received,hashed_bytes=hashed,
            sha256=sha256,writable_verified=True,peer_verified=True,expires_at=grant['expires_at'])

    async def probe(self,node,relation,request_id,grant):
        if self.target_lock.locked():raise TransferError('migration_transfer_target_busy')
        async with self.target_lock:
            value,_=await self._bind(grant,'target',expected='probe',authenticated_relation=relation)
            await self.native.seal(node,relation,request_id,value['task_id'])
            request=target_request(self.store.unseal(self.store.node['private_key']),grant,now=self.wall())
            async with self._remote(value,request) as response:
                if response.status_code!=200:raise TransferError('migration_transfer_direct_probe_failed')
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw)+len(chunk)>CONTROL_BYTES:raise TransferError('migration_transfer_source_reply_too_large')
                    raw.extend(chunk)
                self._verify_source(json.loads(raw),request,value)
            async with self.store.database.connect() as conn:
                kinds=list((await conn.execute(select(s.units.c.resource_kind).where(s.units.c.task_id==value['task_id']).distinct())).scalars())
            staging_roots={}
            for kind in kinds:
                root=self.root_provider()['media' if kind=='media' else 'recordings']
                staging_roots[kind]=await joined_io(files.prepare_probe,root,value['task_id'])
                async with self.store.database.begin() as conn:
                    row=await self._control_row(conn,value)
                    control={**row['payload']['transfer'],'staging_roots':dict(staging_roots)}
                    await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==value['task_id']).values(
                        payload={**row['payload'],'transfer':control}))
                staging_roots[kind]=await joined_io(files.probe,root,value['task_id'],value['target_binding'],staging_roots[kind])
            await self._probed(value,staging_roots=staging_roots)
            return self._reply(request_id,grant,value,state='probed')

    async def copy(self,node,relation,request_id,grant):
        if self.target_lock.locked():raise TransferError('migration_transfer_target_busy')
        async with self.target_lock:
            value,_=await self._bind(grant,'target',expected='copy',authenticated_relation=relation);obj=value['object'];task_id=value['task_id']
            self._alive(value)
            row=await self._confirm(value)
            expected_root=row['payload']['transfer'].get('staging_roots',{}).get(obj['resource_kind'])
            if not expected_root:raise TransferError('migration_transfer_probe_required')
            async with self.store.database.begin() as conn:
                await self.store.lock(conn)
                old=(await conn.execute(select(s.storage_journal).where(s.storage_journal.c.task_id==task_id,
                    s.storage_journal.c.logical_id==obj['logical_id']))).mappings().first()
                expected=dict(unit_id=obj['unit_id'],master_id=value['master_id'],source_id=value['source_id'],target_id=value['target_id'],
                    resource_kind=obj['resource_kind'],object_id=obj['source_object'],physical_path=obj['target_path'],
                    source_version=obj['source_version'],expected_bytes=obj['expected_bytes'])
                if old:
                    if old['state'] not in {'receiving','verified'} or any(old[name]!=item for name,item in expected.items()):
                        raise TransferError('migration_transfer_staging_journal_changed')
                else:await conn.execute(insert(s.storage_journal).values(task_id=task_id,logical_id=obj['logical_id'],
                    **expected,state='receiving',updated_at=int(self.wall())))
            root=self.root_provider()['media' if obj['resource_kind']=='media' else 'recordings']
            descriptor,info=await joined_io(files.open_stage,root,task_id,obj,expected_root,on_cancel=lambda pair:os.close(pair[0]))
            checksum=hashlib.sha256();hashed=0;offset=info.st_size;identity=native(info);cache_key=(task_id,obj['logical_id'])
            try:
                async with self.store.database.begin() as conn:
                    await self._control_row(conn,value)
                    entry=(await conn.execute(select(s.storage_journal).where(s.storage_journal.c.task_id==task_id,
                        s.storage_journal.c.logical_id==obj['logical_id']).with_for_update())).mappings().one()
                    current_identity=files.stage_identity(info)
                    if not entry['stage_identity'] and info.st_size:
                        raise TransferError('migration_transfer_staging_identity_missing')
                    if entry['stage_identity'] and entry['stage_identity']!=current_identity:
                        raise TransferError('migration_transfer_staging_identity_changed')
                    await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==task_id,
                        s.storage_journal.c.logical_id==obj['logical_id']).values(stage_identity=current_identity))
                if self.hash_state and self.hash_state[:2]==(cache_key,identity):checksum,hashed=self.hash_state[2:]
                while hashed<offset:
                    self._alive(value)
                    data=await joined_io(os.pread,descriptor,min(CHUNK_BYTES,offset-hashed),hashed)
                    if not data:raise TransferError('migration_transfer_staging_truncated')
                    checksum.update(data);hashed+=len(data)
                    if self.wall()+1>=value['expires_at']:
                        return self._reply(request_id,grant,value,state='reconciling',received=offset,hashed=hashed)
                if offset<obj['expected_bytes']:
                    from app.services.resource_transfer_protocol import WINDOW_BYTES
                    length=min(WINDOW_BYTES,obj['expected_bytes']-offset)
                    request=target_request(self.store.unseal(self.store.node['private_key']),grant,offset=offset,length=length,now=self.wall())
                    async with self._remote(value,request) as response:
                        if response.status_code!=200 or response.headers.get('content-encoding') not in (None,'identity'):
                            raise TransferError('migration_transfer_direct_copy_failed')
                        try:self._verify_source(json.loads(p.decode(response.headers['x-frontiercloud-transfer-proof'])),request,value)
                        except (ValueError,KeyError) as exc:raise TransferError('migration_transfer_source_proof_invalid') from exc
                        if response.headers.get('content-length')!=str(length):raise TransferError('migration_transfer_length_invalid')
                        received=0
                        async for data in response.aiter_bytes(CHUNK_BYTES):
                            self._alive(value)
                            if not data or received+len(data)>length:raise TransferError('migration_transfer_length_invalid')
                            await joined_io(self._write,descriptor,data,offset)
                            checksum.update(data);offset+=len(data);hashed+=len(data);received+=len(data)
                        if received!=length:raise TransferError('migration_transfer_incomplete')
                await joined_io(os.fsync,descriptor)
                await self._confirm(value)
                verified=offset==obj['expected_bytes']
                if verified and checksum.hexdigest()!=obj['sha256']:raise TransferError('migration_transfer_hash_mismatch')
                if verified:
                    async with self.store.database.begin() as conn:
                        await self._control_row(conn,value)
                        await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==task_id,
                            s.storage_journal.c.logical_id==obj['logical_id']).values(state='verified',sha256=checksum.hexdigest(),
                                publication={'verified_native':native(os.fstat(descriptor))},updated_at=int(self.wall())))
                return self._reply(request_id,grant,value,state='verified' if verified else 'receiving',received=offset,hashed=hashed,
                    sha256=checksum.hexdigest() if verified else '')
            finally:
                try:
                    await joined_io(os.fsync,descriptor)
                    current=os.fstat(descriptor)
                    self.hash_state=(cache_key,native(current),checksum.copy(),hashed) if hashed<=current.st_size else None
                finally:os.close(descriptor)

    @staticmethod
    def _write(descriptor,data,offset):
        usage=os.fstatvfs(descriptor)
        if usage.f_bavail*usage.f_frsize<PHYSICAL_RESERVE_BYTES+len(data):raise TransferError('migration_transfer_physical_capacity_changed')
        files.write(descriptor,data,offset)
