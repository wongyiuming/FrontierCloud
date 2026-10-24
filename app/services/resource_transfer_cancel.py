"""Explicit two-peer stop, owned staging cleanup and exact quota settlement."""
import json
import time
import uuid

import httpx

from sqlalchemy import delete,func,select,update

from app.core.joined_io import joined_io
from app.services import resource_migration_schema as s,resource_transfer_files as files
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_control_client import signed_request
from app.services.resource_observation import digest
from app.services.resource_plan_store import relation_binding
from app.services.resource_transfer_master import TransferMaster
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError,identifier


class CancelPeers:
    def __init__(self,transfers):self.t=transfers;self.store=transfers.store

    async def _row(self,conn,grant):
        row=await self.t._control_row(conn,grant)
        if row['state']!='aborting':raise TransferError('migration_abort_state_changed')
        if await conn.scalar(select(s.objects.c.logical_id).where(s.objects.c.task_id==grant['task_id']).limit(1)):
            raise TransferError('migration_abort_native_objects_unexpected')
        for table,condition in (
            (s.units,s.units.c.state.not_in(['pending','copying','verified','cancelled'])),
            (s.storage_journal,s.storage_journal.c.state.not_in(['receiving','verified','aborting','aborted'])),
        ):
            if await conn.scalar(select(next(iter(table.primary_key.columns))).where(table.c.task_id==grant['task_id'],condition).limit(1)):
                raise TransferError('migration_abort_published_material')
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==grant['task_id']))).one()
        scopes=await conn.scalar(select(func.count()).select_from(s.scopes).where(s.scopes.c.task_id==grant['task_id'],s.scopes.c.member_id==row['member_id']))
        if tuple(counts)!=(row['expected_units'],row['expected_objects'],row['expected_bytes']) or scopes!=row['expected_units']:
            raise TransferError('migration_abort_scope_inconsistent')
        return row

    def _reply(self,request_id,grant,row,state,*,processed=0):
        return dict(format='frontiercloud-python-transfer-cancel',version=1,request_id=request_id,
            node_id=row['member_id'],task_id=grant['task_id'],master_id=grant['master_id'],side=row['side'],epoch=grant['epoch'],
            binding_sha256=grant[row['side']+'_binding'],state=state,processed_objects=processed,
            execution_authorized=False,source_files_removed=False)

    async def _begin(self,node,relation,request_id,grant):
        identifier(request_id)
        raw=grant.get('payload',{}) if isinstance(grant,dict) else {}
        side='source' if node['node_id']==raw.get('source_id') else 'target'
        value,_=await self.t._bind(grant,side,expected='abort',authenticated_relation=relation)
        async with self.store.database.begin() as conn:
            row=await self.t.native._row(conn,node,relation,value['task_id'])
        return value,row

    async def stop(self,node,relation,request_id,grant):
        value,row=await self._begin(node,relation,request_id,grant)
        if row['state']=='aborted':return self._reply(request_id,value,row,'aborted')
        if self.t.target_lock.locked() or self.t.source_lock.locked():return self._reply(request_id,value,row,'waiting_writer')
        async with self.store.database.begin() as conn:
            row=await self._row(conn,value)
            readers=await conn.scalar(select(func.count()).select_from(s.read_leases).where(s.read_leases.c.member_id==row['member_id'],
                s.read_leases.c.task_id==value['task_id'],s.read_leases.c.purpose=='migration',s.read_leases.c.expires_at>self.t.wall()))
            if readers:return self._reply(request_id,value,row,'waiting_reader')
            control={**row['payload']['transfer'],'stop_complete':True}
            await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==value['task_id']).values(
                payload={**row['payload'],'transfer':control}))
            await self.store.log(conn,'migration_transfer_stopped',value['master_id'],task_id=value['task_id'],side=row['side'])
        return self._reply(request_id,value,row,'stopped')

    async def clean(self,node,relation,request_id,grant):
        value,row=await self._begin(node,relation,request_id,grant)
        if row['state']=='aborted':return self._reply(request_id,value,row,'aborted')
        if self.t.target_lock.locked() or self.t.source_lock.locked():return self._reply(request_id,value,row,'waiting_writer')
        async with self.t.target_lock:
            async with self.store.database.begin() as conn:
                row=await self._row(conn,value)
                if row['payload']['transfer'].get('stop_complete') is not True:
                    raise TransferError('migration_abort_stop_required')
                readers=await conn.scalar(select(func.count()).select_from(s.read_leases).where(s.read_leases.c.task_id==value['task_id'],
                    s.read_leases.c.member_id==row['member_id'],s.read_leases.c.purpose=='migration',s.read_leases.c.expires_at>self.t.wall()))
                if readers:return self._reply(request_id,value,row,'waiting_reader')
                entry=(await conn.execute(select(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id'],
                    s.storage_journal.c.state!='aborted').order_by(s.storage_journal.c.logical_id).limit(1))).mappings().first()
            if row['side']=='target':
                if not self.t.traffic.snapshot()['can_run']:return self._reply(request_id,value,row,'waiting_low_traffic')
                if entry:
                    await self._file(value,row,dict(entry))
                    return self._reply(request_id,value,row,'cleaning',processed=1)
                if await self._directory(value,row):return self._reply(request_id,value,row,'cleaning')
            elif entry:raise TransferError('migration_abort_source_journal_unexpected')
            await self._release(value)
            return self._reply(request_id,value,row,'aborted')

    async def _file(self,grant,row,entry):
        task_id=grant['task_id'];logical=entry['logical_id']
        identifier(logical,long=entry['resource_kind']=='media');identifier(entry['unit_id'],long=True)
        async with self.store.database.begin() as conn:
            await self._row(conn,grant)
            binding=(await conn.execute(select(s.peer_bindings).where(s.peer_bindings.c.task_id==task_id,
                s.peer_bindings.c.logical_id==logical))).mappings().first()
            if not binding or digest(json.loads(binding['binding_json']))!=binding['binding_sha256']:
                raise TransferError('migration_abort_binding_missing')
            obj=json.loads(binding['binding_json'])['object']
            from app.services.resource_transfer_protocol import immutable
            if {key:value for key,value in json.loads(binding['binding_json']).items() if key!='object'}!={
                    key:value for key,value in immutable(grant).items() if key!='object'}:
                raise TransferError('migration_abort_binding_changed')
            unit=(await conn.execute(select(s.units).where(s.units.c.task_id==task_id,s.units.c.unit_id==entry['unit_id']))).mappings().first()
            if not unit or unit['source_version']!=obj['unit_version'] or unit['parent_path']!=obj['target_path'].rsplit('/',1)[0]:
                raise TransferError('migration_abort_binding_changed')
            if any(entry[name]!=obj[field] for name,field in (('logical_id','logical_id'),('unit_id','unit_id'),
                ('object_id','source_object'),('physical_path','target_path'),('source_version','source_version'),('expected_bytes','expected_bytes'))):
                raise TransferError('migration_abort_binding_changed')
        root=self.t.root_provider()['media' if entry['resource_kind']=='media' else 'recordings']
        expected_root=row['payload']['transfer'].get('staging_roots',{}).get(entry['resource_kind'])
        if expected_root is None:raise TransferError('migration_abort_root_proof_missing')
        observed=await joined_io(files.inspect_stage,root,task_id,obj,expected_root)
        if observed:
            if entry['stage_identity'] and entry['stage_identity']!=observed['identity']:
                raise TransferError('migration_abort_stage_changed')
            if not entry['stage_identity'] and observed['bytes']:
                raise TransferError('migration_abort_stage_identity_missing')
            if entry['state']=='aborting' and entry['cleanup_version']!=observed['version']:
                raise TransferError('migration_abort_stage_changed')
        async with self.store.database.begin() as conn:
            await self._row(conn,grant)
            current=(await conn.execute(select(s.storage_journal).where(s.storage_journal.c.task_id==task_id,
                s.storage_journal.c.logical_id==logical).with_for_update())).mappings().one()
            if current['state']!=entry['state'] or current['stage_identity']!=entry['stage_identity']:
                raise TransferError('migration_abort_journal_changed')
            identity=observed['identity'] if observed else entry['stage_identity']
            version=observed['version'] if observed else entry['cleanup_version']
            await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==task_id,
                s.storage_journal.c.logical_id==logical).values(state='aborting',stage_identity=identity,cleanup_version=version))
            await self.store.log(conn,'migration_stage_abort_intent',grant['master_id'],task_id=task_id,logical_id=logical)
        if observed:await joined_io(files.unlink_stage,root,task_id,obj,expected_root,identity,version)
        async with self.store.database.begin() as conn:
            await self._row(conn,grant)
            await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==task_id,
                s.storage_journal.c.logical_id==logical).values(state='aborted',updated_at=int(self.t.wall())))
            await self.store.log(conn,'migration_stage_aborted',grant['master_id'],task_id=task_id,logical_id=logical)

    async def _directory(self,grant,row):
        task_id=grant['task_id'];control=row['payload']['transfer'];after=control.get('cancel_unit_after','')
        async with self.store.database.connect() as conn:
            unit=(await conn.execute(select(s.units).where(s.units.c.task_id==task_id,s.units.c.unit_id>after)
                .order_by(s.units.c.unit_id).limit(1))).mappings().first()
        if unit:
            identifier(unit['unit_id'],long=True)
            root=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
            expected=control.get('staging_roots',{}).get(unit['resource_kind'])
            # With no probe and no journal, this can only remove an empty directory.
            await joined_io(files.remove_unit,root,task_id,unit['unit_id'],expected)
            async with self.store.database.begin() as conn:
                current=await self._row(conn,grant)
                updated={**current['payload']['transfer'],'cancel_unit_after':unit['unit_id']}
                await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==task_id).values(
                    payload={**current['payload'],'transfer':updated}))
            return True
        for kind in ('media','recording'):
            root=self.t.root_provider()['media' if kind=='media' else 'recordings']
            await joined_io(files.finish_task,root,task_id,control.get('staging_roots',{}).get(kind),row['binding_sha256'])
        return False

    async def _release(self,grant):
        async with media_mutation_lock,self.store.database.begin() as conn:
            row=await self._row(conn,grant);task_id=grant['task_id']
            if await conn.scalar(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==task_id,s.storage_journal.c.state!='aborted').limit(1)):
                raise TransferError('migration_abort_staging_remaining')
            expected=row['expected_bytes'] if row['side']=='target' else 0
            member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==row['member_id']).with_for_update())).mappings().one()
            if member['reserved_bytes']<expected:raise TransferError('migration_abort_capacity_inconsistent')
            if expected:await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==row['member_id']).values(
                reserved_bytes=fs.storage_members.c.reserved_bytes-expected,updated_at=int(self.t.wall())))
            for table in (s.capacity,s.scopes,s.units):await conn.execute(delete(table).where(table.c.task_id==task_id))
            await conn.execute(delete(s.drains).where(s.drains.c.member_id==row['member_id'],s.drains.c.owner_id==task_id,s.drains.c.reason=='migration'))
            await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==task_id).values(state='aborted',
                revision=row['revision']+1,updated_at=int(self.t.wall())))
            await self.store.log(conn,'migration_transfer_aborted',grant['master_id'],task_id=task_id,side=row['side'],reserved_bytes=expected)


class Cancellation:
    def __init__(self,store,transport,*,traffic=None,wall=time.time):
        self.master=TransferMaster(store,transport,traffic=traffic,wall=wall)
        self.store,self.transport,self.wall=store,transport,wall

    async def request(self,task_id,expected_revision,actor):
        if type(expected_revision) is not int or expected_revision<1:raise TransferError('migration_abort_revision_invalid')
        # Prepared tasks may have private probe material but no executing runner.
        async with media_mutation_lock,self.store.database.begin() as conn:
            task=await self.master.journal._task(conn,task_id)
            if task['revision']!=expected_revision:raise TransferError('migration_abort_task_changed')
            if task['state']=='prepared':
                await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task_id).values(state='cancelling',
                    revision=task['revision']+1,updated_at=int(self.wall())))
                await conn.execute(update(s.runners).where(s.runners.c.task_id==task_id).values(expires_at=0))
                await self.store.log(conn,'migration_cancel',actor,task_id=task_id)
                return
        await self.master.journal.control(task_id,expected_revision,'cancel',actor)

    async def _call(self,relation,action,grant,side):
        request_id=uuid.uuid4().hex
        value=await signed_request(self.store,relation,self.transport,'/internal/v1/migration/transfer/abort-'+action,
            dict(request_id=request_id,grant=grant),limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_abort')
        expected=dict(format='frontiercloud-python-transfer-cancel',version=1,request_id=request_id,node_id=relation['peer_id'],
            task_id=grant['payload']['task_id'],master_id=self.store.node['node_id'],side=side,epoch=grant['payload']['epoch'],
            binding_sha256=grant['payload'][side+'_binding'],execution_authorized=False,source_files_removed=False)
        if (not isinstance(value,dict) or value.keys()!={*expected,'state','processed_objects'} or
                any(value.get(key)!=item for key,item in expected.items()) or type(value.get('version')) is not int or
                type(value.get('epoch')) is not int or type(value.get('processed_objects')) is not int or not 0<=value['processed_objects']<=1 or
                value['execution_authorized'] is not False or value['source_files_removed'] is not False or
                value.get('state') not in {'stopped','aborted','waiting_reader','waiting_writer','cleaning','waiting_low_traffic'}):
            raise TransferError('migration_abort_reply_invalid')
        return value

    async def advance(self,task_id,actor):
        helper=self.master.preparation
        identifier(task_id)
        from app.services.resource_cancel_intent import published
        async with self.store.database.begin() as conn:
            current=await self.master.journal._task(conn,task_id)
            if current['state']!='cancelled' and published(current):
                raise TransferError('migration_cancel_execution_lease_required')
        async with helper.lock,helper._running(task_id):
            try:
                async with self.store.database.begin() as conn:
                    task=await helper._locked(conn,task_id)
                    if task['state']=='cancelled':return dict(task_id=task_id,state='cancelled')
                    if task['state']!='cancelling':raise TransferError('migration_abort_request_required')
                    if task['committed_bytes'] or await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==task_id,
                        s.units.c.state.not_in(['pending','copying','verified','cancelled'])).limit(1)):
                        raise TransferError('migration_abort_committed_requires_reconciliation')
                    relations={}
                    for side in ('source','target'):
                        relation=(await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==task[side+'_relationship']))).mappings().one()
                        if relation_binding(relation)!=task['plan']['facts']['relationships'][task[side+'_id']]:
                            raise TransferError('migration_abort_members_changed')
                        relations[task[side+'_id']]=relation
                grant=self.master._grant(task,relations,helper.lease[1],helper.runner_id,'abort')
                receipts={}
                for side in ('source','target'):
                    receipts[side]=await self._call(relations[task[side+'_id']],'stop',grant,side)
                if any(item['state'] not in {'stopped','aborted'} for item in receipts.values()):
                    await helper._save(task_id,phase='cancel_waiting',cancel={side:value['state'] for side,value in receipts.items()})
                    return dict(task_id=task_id,state='cancelling',peers={side:value['state'] for side,value in receipts.items()})
                # Target must finish owned staging cleanup before Source releases.
                for side in ('target','source'):
                    value=await self._call(relations[task[side+'_id']],'clean',grant,side)
                    if value['state']!='aborted':
                        await helper._save(task_id,phase='cancel_cleaning',cancel={side:value['state']})
                        return dict(task_id=task_id,state='cancelling',peers={side:value['state']})
                async with media_mutation_lock,self.store.database.begin() as conn:
                    task=await helper._locked(conn,task_id)
                    if task['state']!='cancelling' or task['committed_bytes']:raise TransferError('migration_abort_task_changed')
                    capacity=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==task_id))).mappings().first()
                    member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==task['target_id']).with_for_update())).mappings().first()
                    if not capacity or capacity['member_id']!=task['target_id'] or capacity['reserved_bytes']!=task['total_bytes'] or not member or member['reserved_bytes']<task['total_bytes']:
                        raise TransferError('migration_abort_capacity_inconsistent')
                    await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==task['target_id']).values(
                        reserved_bytes=fs.storage_members.c.reserved_bytes-task['total_bytes'],updated_at=int(self.wall())))
                    await conn.execute(update(s.objects).where(s.objects.c.task_id==task_id).values(state='cancelled',updated_at=int(self.wall())))
                    await conn.execute(update(s.units).where(s.units.c.task_id==task_id).values(state='cancelled'))
                    for table in (s.scopes,s.capacity):await conn.execute(delete(table).where(table.c.task_id==task_id))
                    await conn.execute(delete(s.drains).where(s.drains.c.owner_id==task_id,s.drains.c.reason=='migration'))
                    await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task_id).values(state='cancelled',
                        plan={**task['plan'],'phase':'cancelled'},revision=task['revision']+1,error_code='',updated_at=int(self.wall())))
                    await self.store.log(conn,'migration_transfer_cancelled',actor,task_id=task_id)
                return dict(task_id=task_id,state='cancelled')
            except (p.ProtocolError,OSError,TimeoutError,httpx.HTTPError) as exc:
                await helper._error(task_id,exc)
                raise
