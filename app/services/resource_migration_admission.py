"""Durable Master/native preparation with bounded metadata and restart recovery.

Prepared tasks cannot be claimed by the transfer journal. Verified staging,
peer connectivity and narrowly scoped transfer capabilities are separate gates.
No media bytes, premaster or filesystem mutation pass through this coordinator.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import re
import time
import uuid

import httpx
from sqlalchemy import delete, func, insert, select, update

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p, schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_inventory_client import InventoryClient
from app.services.resource_migration_planning import extend
from app.services.resource_observation import digest
from app.services.resource_peer_reservations import INITIAL_UNITS_SHA256, unit_transcript
from app.services.resource_plan_store import BATCH, PlanError, PlanStore, relation_binding
from app.services.resource_pool import PHYSICAL_RESERVE_BYTES
from app.services.resource_registry import RegistryClient
from app.services.resource_reservation_client import ReservationClient


class AdmissionError(PlanError):
    pass


class Admission:
    def __init__(self,store,transport,*,preview_epoch=None,traffic=None,wall=time.time):
        from app.services.business_traffic import monitor
        self.store,self.transport,self.preview_epoch,self.wall=store,transport,preview_epoch,wall
        self.traffic=monitor if traffic is None else traffic
        self.spool=PlanStore(store,wall=wall)
        self.lock=asyncio.Lock()
        self.runner_id=uuid.uuid4().hex
        self.lease=None

    async def _task(self,conn,task_id):
        node=await self.store.lock(conn)
        task=(await conn.execute(select(s.tasks).where(s.tasks.c.task_id==task_id).with_for_update())).mappings().first()
        if node['role']!='Master' or task is None or task['master_id']!=node['node_id']:
            raise AdmissionError('migration_admission_unavailable')
        return dict(task)

    async def _locked(self,conn,task_id):
        task=await self._task(conn,task_id)
        lease=(await conn.execute(select(s.runners).where(s.runners.c.task_id==task_id).with_for_update())).mappings().first()
        if (not self.lease or self.lease[0]!=task_id or not lease or lease['runner_id']!=self.runner_id or
                lease['epoch']!=self.lease[1] or lease['expires_at']<=self.wall()):
            raise AdmissionError('migration_admission_lease_lost')
        return task

    @asynccontextmanager
    async def _running(self,task_id):
        async with self.store.database.begin() as conn:
            await self._task(conn,task_id)
            old=(await conn.execute(select(s.runners).where(s.runners.c.task_id==task_id).with_for_update())).mappings().first()
            if old and old['expires_at']>self.wall():
                raise AdmissionError('migration_admission_busy')
            epoch=old['epoch']+1 if old else 1
            values=dict(runner_id=self.runner_id,epoch=epoch,expires_at=int(self.wall())+45)
            if old:await conn.execute(update(s.runners).where(s.runners.c.task_id==task_id).values(**values))
            else:await conn.execute(insert(s.runners).values(task_id=task_id,**values))
        self.lease=(task_id,epoch)
        async def keepalive():
            while True:
                await asyncio.sleep(10)
                async with self.store.database.begin() as conn:
                    await self._locked(conn,task_id)
                    await conn.execute(update(s.runners).where(s.runners.c.task_id==task_id).values(expires_at=int(self.wall())+45))
        heartbeat=asyncio.create_task(keepalive(),name='migration-admission-lease')
        try:
            yield
            if heartbeat.done():heartbeat.result()
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat,return_exceptions=True)
            async with self.store.database.begin() as conn:
                await conn.execute(update(s.runners).where(s.runners.c.task_id==task_id,
                    s.runners.c.runner_id==self.runner_id,s.runners.c.epoch==epoch).values(expires_at=0))
            self.lease=None

    async def _create(self,plan_id,approved_sha256,actor):
        async with media_mutation_lock,self.store.database.begin() as conn:
            node=await self.store.lock(conn)
            old=(await conn.execute(select(s.tasks).where(s.tasks.c.task_id==plan_id))).mappings().first()
            if old:
                if old['master_id']!=node['node_id'] or old['plan_sha256']!=approved_sha256:
                    raise AdmissionError('migration_admission_request_changed')
                return
            preview=(await conn.execute(select(s.plans).where(s.plans.c.plan_id==plan_id).with_for_update())).mappings().first()
            if (not preview or preview['master_id']!=node['node_id'] or preview['state']!='ready' or
                    preview['expires_at']<=self.wall() or preview['plan_sha256']!=approved_sha256):
                raise AdmissionError('migration_admission_preview_invalid')
            header=json.loads(preview['header_json'])
            if (not self.preview_epoch or header['epoch']!=self.preview_epoch or header['blocker_count'] or
                    header['blockers_complete'] is not True or header['inventory_complete'] is not True):
                raise AdmissionError('migration_admission_preview_invalid')
            facts,_,relations=await self.spool.facts(preview['source_id'],preview['target_id'],conn=conn)
            expected={name:header[name] for name in facts}
            review=digest(dict(facts=expected,catalog_sha256=preview['catalog_sha256'],proofs_sha256=preview['proofs_sha256'],
                registries=header['registries'],object_count=preview['object_count'],unit_count=preview['unit_count'],total_bytes=preview['total_bytes']))
            if facts!=expected or review!=approved_sha256:
                raise AdmissionError('migration_admission_preview_changed')
            if not 0<preview['unit_count']<=preview['object_count'] or preview['total_bytes']<=0:
                raise AdmissionError('migration_admission_preview_invalid')
            now=int(self.wall())
            plan=dict(format='frontiercloud-python-admission',version=1,facts=facts,phase='local_scopes',
                      local_installed=False,units_sha256='',peers={},unit_count=preview['unit_count'])
            await conn.execute(insert(s.tasks).values(task_id=plan_id,master_id=node['node_id'],
                source_id=preview['source_id'],target_id=preview['target_id'],
                source_relationship=relations[preview['source_id']]['relationship_id'],
                target_relationship=relations[preview['target_id']]['relationship_id'],plan_sha256=approved_sha256,
                plan=plan,state='admitting',object_count=preview['object_count'],total_bytes=preview['total_bytes'],created_at=now,updated_at=now))
            await conn.execute(insert(s.capacity).values(task_id=plan_id,member_id=preview['target_id'],reserved_bytes=preview['total_bytes'],updated_at=now))
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==preview['target_id']).values(
                reserved_bytes=fs.storage_members.c.reserved_bytes+preview['total_bytes'],updated_at=now))
            for member in (preview['source_id'],preview['target_id']):
                await conn.execute(insert(s.drains).values(member_id=member,owner_id=plan_id,reason='migration',created_at=now))
            await conn.execute(update(s.plans).where(s.plans.c.plan_id==plan_id).values(state='admitted'))
            await self.store.log(conn,'migration_admission_started',actor,task_id=plan_id,plan_sha256=approved_sha256)

    async def _units(self,task_id):
        after='';unit=None;checksum=None
        while True:
            async with self.store.database.connect() as conn:
                rows=list((await conn.execute(select(s.plan_bindings).where(s.plan_bindings.c.plan_id==task_id,
                    (s.plan_bindings.c.unit_id>after[0]) | ((s.plan_bindings.c.unit_id==after[0]) &
                        (s.plan_bindings.c.logical_id>after[1])) if after else True)
                    .order_by(s.plan_bindings.c.unit_id,s.plan_bindings.c.logical_id).limit(BATCH))).mappings())
            if not rows:break
            for row in rows:
                snapshot=json.loads(row['object_json'])
                if (not p.OBJECT_ID.fullmatch(row['source_sha256']) or
                        snapshot['source_proof']['result']['sha256']!=row['source_sha256']):
                    raise AdmissionError('migration_admission_content_proof_missing')
                if unit is None or unit['unit_id']!=row['unit_id']:
                    if unit:
                        yield {**unit,'source_version':checksum.hexdigest()}
                    unit=dict(unit_id=row['unit_id'],resource_kind=row['resource_kind'],source_parent=snapshot['parent_path'],
                        target_parent=snapshot['target_parent'],object_count=0,total_bytes=0)
                    checksum=hashlib.sha256()
                if (unit['resource_kind']!=row['resource_kind'] or unit['source_parent']!=snapshot['parent_path'] or
                        unit['target_parent']!=snapshot['target_parent']):
                    raise AdmissionError('migration_admission_unit_changed')
                unit['object_count']+=1;unit['total_bytes']+=row['expected_bytes']
                extend(checksum,dict(logical_id=row['logical_id'],source_object=row['source_object'],
                    source_sha256=row['source_sha256'],source_version=snapshot['source_proof']['result']['source_version']))
            after=(rows[-1]['unit_id'],rows[-1]['logical_id'])
        if unit:yield {**unit,'source_version':checksum.hexdigest()}

    async def _save(self,task_id,*,phase=None,**plan_values):
        async with self.store.database.begin() as conn:
            task=await self._locked(conn,task_id)
            plan={**task['plan'],**plan_values}
            if phase:plan['phase']=phase
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task_id).values(
                plan=plan,revision=task['revision']+1,error_code='',updated_at=int(self.wall())))
            return {**task,'plan':plan}

    async def _install(self,task):
        task_id=task['task_id'];transcript=INITIAL_UNITS_SHA256;count=objects=total=0
        async for unit in self._units(task_id):
            async with media_mutation_lock,self.store.database.begin() as conn:
                current=await self._locked(conn,task_id)
                if current['state']!='admitting':raise AdmissionError('migration_admission_state_changed')
                values=dict(resource_kind=unit['resource_kind'],parent_path=unit['source_parent'],source_version=unit['source_version'],
                    state='pending',object_count=unit['object_count'],total_bytes=unit['total_bytes'])
                old=(await conn.execute(select(s.units).where(s.units.c.task_id==task_id,s.units.c.unit_id==unit['unit_id']))).mappings().first()
                if old:
                    if any(old[name]!=value for name,value in values.items()):raise AdmissionError('migration_admission_unit_changed')
                else:await conn.execute(insert(s.units).values(task_id=task_id,unit_id=unit['unit_id'],**values))
                for member,path in ((task['source_id'],unit['source_parent']),(task['target_id'],unit['target_parent'])):
                    scope_id=digest([task_id,member,unit['resource_kind'],path])
                    scope=(await conn.execute(select(s.scopes).where(s.scopes.c.scope_id==scope_id))).mappings().first()
                    if scope is None:await conn.execute(insert(s.scopes).values(scope_id=scope_id,task_id=task_id,
                        member_id=member,resource_kind=unit['resource_kind'],parent_path=path,created_at=int(self.wall())))
                    elif any(scope[name]!=value for name,value in dict(task_id=task_id,member_id=member,
                        resource_kind=unit['resource_kind'],parent_path=path).items()):raise AdmissionError('migration_admission_scope_changed')
            count+=1;objects+=unit['object_count'];total+=unit['total_bytes'];transcript=unit_transcript(transcript,unit)
        if (count,objects,total)!=(task['plan']['unit_count'],task['object_count'],task['total_bytes']):
            raise AdmissionError('migration_admission_unit_totals_changed')
        return await self._save(task_id,phase='native_prepare',local_installed=True,units_sha256=transcript)

    def _request(self,task,side):
        return {**{name:task[name] for name in ('task_id','master_id','source_id','target_id','source_relationship',
            'target_relationship','plan_sha256','object_count','total_bytes')},
            'side':side,'unit_count':task['plan']['unit_count'],'units_sha256':task['plan']['units_sha256']}

    async def _relations(self,task):
        facts,_,relations=await self.spool.facts(task['source_id'],task['target_id'],task_id=task['task_id'])
        if facts!=task['plan']['facts']:raise AdmissionError('migration_admission_members_changed')
        return relations

    def _sealed(self,value,request):
        if value['state']!='prepared' or any(value[name]!=request[name] for name in ('unit_count','object_count','total_bytes','units_sha256')):
            raise AdmissionError('migration_admission_native_not_prepared')

    async def _recheck(self,task,relations,requests):
        task_id=task['task_id'];source,target=task['source_id'],task['target_id']
        async with self.store.database.connect() as conn:
            preview=(await conn.execute(select(s.plans).where(s.plans.c.plan_id==task_id))).mappings().one()
        header=json.loads(preview['header_json']);registries={};inventories={}
        for member in (source,target):
            await self.traffic.wait()
            await self.transport.identity(relations[member]['peer_endpoint'],expected_id=member,expected_key=relations[member]['peer_key'],role='Follower')
            async def registered(value):await self.spool.registration(task_id,member,source,value,verify=True)
            fresh=await RegistryClient(self.store,relations[member],self.transport).read(registered,reservation=requests[member])
            if any(fresh[name]!=header['registries'][member][name] for name in ('count','sha256')):
                raise AdmissionError('migration_admission_registration_changed')
            registries[member]=fresh
        # Reuse one bounded scan namespace; retries never accumulate tree copies.
        while True:
            async with self.store.database.begin() as conn:
                await self._locked(conn,task_id)
                keys=list((await conn.execute(select(s.plan_paths.c.path_locator).where(s.plan_paths.c.plan_id==task_id,
                    s.plan_paths.c.entry_kind=='admission').limit(256))).scalars())
                if keys:await conn.execute(delete(s.plan_paths).where(s.plan_paths.c.plan_id==task_id,
                    s.plan_paths.c.entry_kind=='admission',s.plan_paths.c.path_locator.in_(keys)))
            if not keys:break
        async with self.store.database.begin() as conn:
            await self._locked(conn,task_id)
            await conn.execute(update(s.plan_bindings).where(s.plan_bindings.c.plan_id==task_id).values(native_seen=0))
        for member in (source,target):
            inventories[member]={}
            for scope in ('data','media','recordings'):
                await self.traffic.wait()
                async def observe(value):
                    await self.spool.native_entry(task_id,member,source,task['source_relationship'],scope,value,observation_round='admission')
                proof=await InventoryClient(self.store,relations[member],self.transport).read(scope,observe)
                if proof['header']['incarnation']!=registries[member]['inventory_incarnation']:
                    raise AdmissionError('migration_admission_peer_restarted')
                inventories[member][scope]=proof
        async with self.store.database.connect() as conn:
            missing=await conn.scalar(select(func.count()).select_from(s.plan_bindings).where(s.plan_bindings.c.plan_id==task_id,s.plan_bindings.c.native_seen!=1))
        if missing:raise AdmissionError('migration_admission_source_missing')
        for member in (source,target):
            async def unchanged(_value):pass
            fresh=await RegistryClient(self.store,relations[member],self.transport).read(unchanged,reservation=requests[member])
            if fresh!=registries[member]:raise AdmissionError('migration_admission_registration_changed')
            self._sealed(await ReservationClient(self.store,relations[member],self.transport,requests[member]).seal(),requests[member])
        native_free=min(proof['header']['physical_free_bytes'] for proof in inventories[target].values())
        if native_free-PHYSICAL_RESERVE_BYTES<task['total_bytes']:
            raise AdmissionError('migration_admission_physical_capacity_changed')
        # No peer I/O while holding the final Master mutation fence/transaction.
        async with media_mutation_lock:
            catalog=hashlib.sha256()
            async for entry in self.spool.catalog(source,task['source_relationship'],task['target_relationship']):extend(catalog,entry)
            async with self.store.database.begin() as conn:
                current=await self._locked(conn,task_id)
                facts,_,_=await self.spool.facts(source,target,conn=conn,task_id=task_id)
                if (current['state']!='admitting' or facts!=task['plan']['facts'] or catalog.hexdigest()!=preview['catalog_sha256']):
                    raise AdmissionError('migration_admission_catalog_changed')
                units=(await conn.execute(select(func.count(),func.sum(s.units.c.object_count),func.sum(s.units.c.total_bytes)).where(
                    s.units.c.task_id==task_id,s.units.c.state=='pending'))).one()
                scopes=await conn.scalar(select(func.count()).select_from(s.scopes).where(s.scopes.c.task_id==task_id))
                if tuple(units)!=(task['plan']['unit_count'],task['object_count'],task['total_bytes']) or scopes!=2*task['plan']['unit_count']:
                    raise AdmissionError('migration_admission_scope_changed')
                plan={**current['plan'],'phase':'native_prepared','verified_at':int(self.wall()),'registries':registries,
                      'native_free_bytes':native_free,'writable_verified':False,'execution_authorized':False}
                await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task_id).values(state='prepared',plan=plan,
                    revision=current['revision']+1,error_code='',updated_at=int(self.wall())))
                await conn.execute(delete(s.drains).where(s.drains.c.member_id==target,s.drains.c.owner_id==task_id,s.drains.c.reason=='migration'))
                await self.store.log(conn,'migration_admission_prepared',self.runner_id,task_id=task_id)

    async def queue(self,plan_id,approved_sha256,actor):
        if (not isinstance(plan_id,str) or not p.IDENTIFIER.fullmatch(plan_id) or
                not isinstance(approved_sha256,str) or not p.OBJECT_ID.fullmatch(approved_sha256)):
            raise AdmissionError('migration_admission_request_invalid')
        async with self.lock:await self._create(plan_id,approved_sha256,actor)
        return await self.status(plan_id)

    async def request_cancel(self,task_id,expected_revision,actor):
        if type(expected_revision) is not int or expected_revision<1:raise AdmissionError('migration_admission_revision_invalid')
        async with media_mutation_lock,self.store.database.begin() as conn:
            task=await self._task(conn,task_id)
            if task['revision']!=expected_revision:raise AdmissionError('migration_admission_task_changed')
            if task['state'] not in {'admitting','prepared'}:raise AdmissionError('migration_admission_state_changed')
            runner=(await conn.execute(select(s.runners).where(s.runners.c.task_id==task_id).with_for_update())).mappings().first()
            if runner and runner['expires_at']>self.wall():raise AdmissionError('migration_admission_busy')
            revision=task['revision']+1
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task_id).values(state='admission_aborting',revision=revision,
                error_code='',updated_at=int(self.wall())))
            await self.store.log(conn,'migration_admission_cancel_requested',actor,task_id=task_id)
            return revision

    async def prepare(self,plan_id,approved_sha256,actor,*,revalidate=False):
        if (not isinstance(plan_id,str) or not p.IDENTIFIER.fullmatch(plan_id) or
                not isinstance(approved_sha256,str) or not p.OBJECT_ID.fullmatch(approved_sha256)):
            raise AdmissionError('migration_admission_request_invalid')
        async with self.lock:
            await self._create(plan_id,approved_sha256,actor)
            async with self._running(plan_id):
                try:
                    async with self.store.database.connect() as conn:task=await self._task(conn,plan_id)
                    if task['state']=='prepared':
                        if not revalidate:return await self.status(plan_id)
                        async with media_mutation_lock,self.store.database.begin() as conn:
                            task=await self._locked(conn,plan_id)
                            await self._unstarted(conn,plan_id)
                            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==plan_id).values(state='admitting',
                                revision=task['revision']+1,updated_at=int(self.wall())))
                            task={**task,'state':'admitting'}
                    if task['state']!='admitting':raise AdmissionError('migration_admission_state_changed')
                    # An interrupted observation may leave seen flags cleared.
                    # Unit authority comes from the original immutable proof.
                    task=await self._install(task)
                    relations=await self._relations(task);requests={}
                    for side in ('source','target'):
                        task=await self._save(plan_id,phase=side+'_prepare')
                        member=task[side+'_id'];request=self._request(task,side);requests[member]=request
                        client=ReservationClient(self.store,relations[member],self.transport,request)
                        value=await client.begin()
                        if value['state'] not in {'preparing','prepared'}:raise AdmissionError('migration_admission_native_terminal')
                        async for unit in self._units(plan_id):await client.add_unit(unit)
                        value=await client.seal();self._sealed(value,request)
                        task=await self._save(plan_id,peers={**task['plan']['peers'],side:value['state']})
                    await self._save(plan_id,phase='final_observation')
                    await self._recheck(task,relations,requests)
                except (p.ProtocolError,httpx.HTTPError,OSError,TimeoutError,asyncio.CancelledError) as exc:
                    await self._error(plan_id,exc)
                    raise
            return await self.status(plan_id)

    async def _error(self,task_id,exc):
        code=str(exc) if isinstance(exc,p.ProtocolError) and re.fullmatch(r'migration_[a-z_]{1,53}',str(exc)) else 'migration_admission_interrupted'
        if isinstance(exc,p.ProtocolError) and re.fullmatch(r'Node control HTTP [1-5][0-9]{2}',str(exc)):
            code='migration_admission_peer_http_'+str(exc)[-3:]
        elif isinstance(exc,httpx.TimeoutException):code='migration_admission_peer_timeout'
        elif isinstance(exc,httpx.HTTPError):code='migration_admission_peer_unavailable'
        async with self.store.database.begin() as conn:
            task=await self._locked(conn,task_id)
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task_id).values(error_code=code,
                revision=task['revision']+1,updated_at=int(self.wall())))

    async def cancel_unstarted(self,task_id,actor):
        if not isinstance(task_id,str) or not p.IDENTIFIER.fullmatch(task_id):raise AdmissionError('migration_admission_request_invalid')
        async with self.lock,self._running(task_id):
            try:
                async with media_mutation_lock,self.store.database.begin() as conn:
                    task=await self._locked(conn,task_id)
                    if task['state']=='cancelled':return await self._status_row(task)
                    if task['state'] not in {'admitting','prepared','admission_aborting'}:
                        raise AdmissionError('migration_admission_requires_reconciliation')
                    await self._unstarted(conn,task_id)
                    await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task_id).values(state='admission_aborting',
                        revision=task['revision']+1,updated_at=int(self.wall())))
                    await self.store.log(conn,'migration_admission_cancel_requested',actor,task_id=task_id)
                # A crash during local installation may predate the transcript.
                # Reconstruct it from the durable immutable preview, not counters.
                transcript=INITIAL_UNITS_SHA256;count=objects=total=0
                async for unit in self._units(task_id):
                    transcript=unit_transcript(transcript,unit);count+=1;objects+=unit['object_count'];total+=unit['total_bytes']
                if (count,objects,total)!=(task['plan']['unit_count'],task['object_count'],task['total_bytes']):
                    raise AdmissionError('migration_admission_unit_totals_changed')
                task=await self._save(task_id,phase='native_cancel',units_sha256=transcript)
                for side in ('source','target'):
                    member=task[side+'_id']
                    async with self.store.database.connect() as conn:
                        relation=(await conn.execute(select(fs.relationships).where(
                            fs.relationships.c.relationship_id==task[side+'_relationship']))).mappings().first()
                    if not relation or relation_binding(relation)!=task['plan']['facts']['relationships'][member]:
                        raise AdmissionError('migration_admission_members_changed')
                    value=await ReservationClient(self.store,relation,self.transport,self._request(task,side)).cancel_unstarted()
                    if value['state']!='aborted':raise AdmissionError('migration_admission_native_not_cancelled')
                    task=await self._save(task_id,peers={**task['plan']['peers'],side:'aborted'})
                async with media_mutation_lock,self.store.database.begin() as conn:
                    task=await self._locked(conn,task_id)
                    await self._unstarted(conn,task_id)
                    capacity=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==task_id))).mappings().first()
                    reserved=await conn.scalar(select(fs.storage_members.c.reserved_bytes).where(fs.storage_members.c.member_id==task['target_id']))
                    if (not capacity or capacity['member_id']!=task['target_id'] or capacity['reserved_bytes']!=task['total_bytes'] or
                            reserved is None or reserved<task['total_bytes']):
                        raise AdmissionError('migration_admission_reservation_inconsistent')
                    await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==task['target_id']).values(
                        reserved_bytes=fs.storage_members.c.reserved_bytes-task['total_bytes'],updated_at=int(self.wall())))
                    for table in (s.scopes,s.units,s.capacity):await conn.execute(delete(table).where(table.c.task_id==task_id))
                    await conn.execute(delete(s.drains).where(s.drains.c.owner_id==task_id,s.drains.c.reason=='migration'))
                    await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task_id).values(state='cancelled',
                        plan={**task['plan'],'phase':'aborted'},revision=task['revision']+1,error_code='',updated_at=int(self.wall())))
                    await self.store.log(conn,'migration_admission_cancelled',actor,task_id=task_id)
            except (p.ProtocolError,httpx.HTTPError,OSError,TimeoutError,asyncio.CancelledError) as exc:
                await self._error(task_id,exc)
                raise
        return await self.status(task_id)

    async def _unstarted(self,conn,task_id):
        for table in (s.objects,s.storage_journal,s.peer_bindings):
            if await conn.scalar(select(next(iter(table.primary_key.columns))).where(table.c.task_id==task_id).limit(1)):
                raise AdmissionError('migration_admission_requires_reconciliation')
        if await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==task_id,s.units.c.state!='pending').limit(1)):
            raise AdmissionError('migration_admission_requires_reconciliation')

    async def status(self,task_id):
        if not isinstance(task_id,str) or not p.IDENTIFIER.fullmatch(task_id):raise AdmissionError('migration_admission_request_invalid')
        async with self.store.database.connect() as conn:
            task=await self._task(conn,task_id)
            if task['state'] in {'admitting','prepared','admission_aborting'}:
                capacity=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==task_id))).mappings().first()
                reserved=await conn.scalar(select(fs.storage_members.c.reserved_bytes).where(fs.storage_members.c.member_id==task['target_id']))
                if (not capacity or capacity['member_id']!=task['target_id'] or capacity['reserved_bytes']!=task['total_bytes'] or
                        reserved is None or reserved<task['total_bytes'] or
                        not await conn.scalar(select(s.drains.c.owner_id).where(s.drains.c.member_id==task['source_id'],
                            s.drains.c.owner_id==task_id,s.drains.c.reason=='migration'))):
                    raise AdmissionError('migration_admission_reservation_inconsistent')
                if task['state']=='prepared':
                    counts=(await conn.execute(select(func.count(),func.sum(s.units.c.object_count),func.sum(s.units.c.total_bytes)).where(
                        s.units.c.task_id==task_id,s.units.c.state=='pending'))).one()
                    scopes=await conn.scalar(select(func.count()).select_from(s.scopes).where(s.scopes.c.task_id==task_id))
                    if tuple(counts)!=(task['plan']['unit_count'],task['object_count'],task['total_bytes']) or scopes!=2*task['plan']['unit_count']:
                        raise AdmissionError('migration_admission_scope_changed')
        return await self._status_row(task)

    async def _status_row(self,task):
        return dict(task_id=task['task_id'],state=task['state'],revision=task['revision'],phase=task['plan']['phase'],
            object_count=task['object_count'],unit_count=task['plan']['unit_count'],total_bytes=task['total_bytes'],
            peers=task['plan']['peers'],error_code=task['error_code'],writable_verified=False,execution_authorized=False)
