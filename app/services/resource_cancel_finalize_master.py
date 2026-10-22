"""Bounded whole-task cancellation after both native release witnesses."""
import uuid

from sqlalchemy import delete,func,select,update

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_catalog import CurrentCatalog,MARKER
from app.services.resource_cancel_target_receipts import source_record
from app.services.resource_cancel_terminal_receipts import cleaned_unit,master_task,target_record
from app.services.resource_cancel_unit_master import UnitCancellation
from app.services.resource_cleanup_master import Cleanup
from app.services.resource_control_client import signed_request
from app.services.resource_migration_journal import Lease
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError,identifier

KEY='cancel_finalization'
COMPLETION='cancel_completion'
FORMAT='frontiercloud-python-master-task-cancelled'


class CancellationFinalization:
    def __init__(self,store,transport,**options):
        self.catalog=CurrentCatalog(store,transport,**options);self.t=self.catalog.t
        self.units=UnitCancellation(store,transport,**options);self.cleanup=Cleanup(store,transport,**options)
        self.store,self.transport=store,transport

    def _key(self):return p.public_key(self.store.unseal(self.store.node['private_key']))

    @staticmethod
    def _counts(task):return task['plan']['unit_count'],task['object_count'],task['total_bytes']

    async def _task(self,conn,lease):
        task=await self.catalog._task(conn,lease)
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==lease.task_id))).one()
        objects=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.objects.c.expected_bytes),0)).where(
            s.objects.c.task_id==lease.task_id,s.objects.c.state.in_(['cleaned','cancelled'])))).one()
        total=await conn.scalar(select(func.count()).select_from(s.objects).where(s.objects.c.task_id==lease.task_id))
        if tuple(counts)!=self._counts(task) or tuple(objects)!=(task['object_count'],task['total_bytes']) or total!=task['object_count']:
            raise TransferError('migration_cancel_finalize_roster_incomplete')
        committed,remaining,members=await self.cleanup.cutover._accounting(conn,task)
        scopes=await conn.scalar(select(func.count()).select_from(s.scopes).where(s.scopes.c.task_id==lease.task_id))
        units=await conn.scalar(select(func.count()).select_from(s.units).where(s.units.c.task_id==lease.task_id,s.units.c.state=='cleaned'))
        if (remaining or committed!=task['committed_bytes'] or scopes!=units*2 or members['source']['reserved_bytes']!=0
                or members['target']['used_bytes']<committed):raise TransferError('migration_cancel_finalize_accounting_changed')
        for table in (s.capacity,s.scopes):
            if await conn.scalar(select(next(iter(table.primary_key.columns))).where(table.c.task_id!=lease.task_id,
                    table.c.member_id==task['source_id']).limit(1)):
                raise TransferError('migration_cancel_finalize_foreign_source_work')
        return task

    def _progress(self,task,lease):
        work=task['plan'].get(KEY)
        if KEY in task['plan']:
            expected={'version','epoch','runner_id','after','unit_count','object_count','total_bytes','census_sha256',
                'source_record','target_record','checkpoint'}
            try:
                if (not isinstance(work,dict) or work.keys()!=expected or type(work['version']) is not int or work['version']!=1
                        or any(type(work[name]) is not int or not 0<=work[name]<2**63 for name in ('epoch','unit_count','object_count','total_bytes'))
                        or work['epoch']<1 or any(work[name]>limit for name,limit in zip(('unit_count','object_count','total_bytes'),self._counts(task)))
                        or work['target_record'] is not None and work['source_record'] is None):raise ValueError()
                identifier(work['runner_id']);identifier(work['census_sha256'],long=True)
                if work['after']!='':identifier(work['after'],long=True)
                expected_cursor=dict(format='frontiercloud-python-master-cancel-cursor',version=1,task_id=task['task_id'],
                    plan_sha256=task['plan_sha256'],progress_sha256=digest({**work,'checkpoint':None}))
                if p.verify(self._key(),work['checkpoint'])!=expected_cursor:raise ValueError()
            except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError):
                raise TransferError('migration_cancel_finalize_progress_changed') from None
            if (work['epoch'],work['runner_id'])==(lease.epoch,lease.runner_id):return work
        return dict(version=1,epoch=lease.epoch,runner_id=lease.runner_id,after='',unit_count=0,object_count=0,total_bytes=0,
            census_sha256=digest(['master-cancel-final-census',1]),source_record=work['source_record'] if work else None,
            target_record=work['target_record'] if work else None,checkpoint=None)

    async def _save(self,conn,task,work,lease):
        work={**work,'checkpoint':None}
        checkpoint=p.sign(self.store.unseal(self.store.node['private_key']),dict(format='frontiercloud-python-master-cancel-cursor',
            version=1,task_id=task['task_id'],plan_sha256=task['plan_sha256'],progress_sha256=digest(work)))
        work={**work,'checkpoint':checkpoint}
        await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task['task_id']).values(
            plan={**task['plan'],KEY:work},updated_at=int(self.t.wall())))
        await self.store.log(conn,'migration_cancel_finalization_step',lease.runner_id,task_id=task['task_id'],
            units=work['unit_count'],source_released=work['source_record'] is not None,target_released=work['target_record'] is not None)
        return work

    async def _source(self,conn,task,grant,witness):
        source=source_record(witness,grant,self._counts(task),self._key(),self.t.wall())
        marker=task['plan'].get(MARKER)
        if not isinstance(marker,dict):raise TransferError('migration_cancel_finalize_catalog_required')
        historical=Lease(task['task_id'],marker.get('runner_id'),marker.get('epoch'),task['revision'])
        progress=self.catalog._progress(task,historical)
        if (progress['phase']!='verified' or progress['source_proof']!=witness['source_census']
                or digest(progress)!=source['master_state_sha256'] or progress['catalog_sha256']!=source['catalog_sha256']
                or (progress['retained_objects'],progress['retained_bytes'])!=(source['retained_objects'],source['retained_bytes'])
                or source['committed_bytes']!=task['committed_bytes']):
            raise TransferError('migration_cancel_finalize_catalog_changed')
        await self.catalog._owned(conn,task,progress)
        return source

    def _target(self,record,grant,source,witness):
        return target_record(record,grant,source,witness,self.t.wall())

    async def _call(self,task,relations,grant,side,**fields):
        request_id=uuid.uuid4().hex;value=grant['payload']
        reply=await signed_request(self.store,relations[task[side+'_id']],self.transport,
            '/internal/v1/migration/transfer/cancel-'+side+'-release',dict(request_id=request_id,grant=grant,**fields),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel_finalize')
        expected=dict(format='frontiercloud-python-'+side+'-cancel-release-progress',version=1,request_id=request_id,
            node_id=task[side+'_id'],task_id=task['task_id'],epoch=value['epoch'],expires_at=value['expires_at'],task_completed=False)
        extra={'phase','receipt','agreement','source_census','source_files_removed'} if side=='source' else {
            'phase','receipt','preparation','source_record','media_files_removed'}
        phases={'pending','released'} if side=='source' else {'units','prepared','released'}
        flag='source_files_removed' if side=='source' else 'media_files_removed'
        if (not isinstance(reply,dict) or reply.keys()!={*expected,*extra} or any(reply.get(name)!=item for name,item in expected.items())
                or any(type(reply[name]) is not int for name in ('version','epoch','expires_at')) or reply[flag] is not False
                or reply['task_completed'] is not False or not isinstance(reply['phase'],str) or reply['phase'] not in phases
                or self.t.wall()>=reply['expires_at']):raise TransferError('migration_cancel_finalize_peer_reply_invalid')
        records=('receipt','agreement','source_census') if side=='source' else ('receipt','preparation','source_record')
        if reply['phase']!='released' and any(reply[name] is not None for name in records):
            raise TransferError('migration_cancel_finalize_peer_reply_invalid')
        return reply

    async def _history(self,conn,task,lease,work):
        units=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id>work['after'])
            .order_by(s.units.c.unit_id).limit(4))).mappings().all()
        for unit in units:
            counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.objects.c.expected_bytes),0)).where(
                s.objects.c.task_id==task['task_id'],s.objects.c.unit_id==unit['unit_id'],s.objects.c.state==unit['state']))).one()
            if tuple(counts)!=(unit['object_count'],unit['total_bytes']):raise TransferError('migration_cancel_finalize_unit_objects_changed')
            if unit['state']=='cancelled':proof=self.units._recorded(task,unit)
            else:
                await self.cleanup._task(conn,lease,unit['unit_id']);await self.cleanup._catalog(conn,task,unit)
                proof=cleaned_unit(unit,self.t.wall())
            work={**work,'after':unit['unit_id'],'unit_count':work['unit_count']+1,'object_count':work['object_count']+unit['object_count'],
                'total_bytes':work['total_bytes']+unit['total_bytes'],'census_sha256':digest([work['census_sha256'],unit['unit_id'],proof])}
        return work,bool(units)

    def _recorded(self,task):
        envelope=task['plan'].get(COMPLETION)
        master_task(envelope,self._key(),task,self.t.wall())
        return envelope

    async def advance(self,lease):
        async with self.store.database.begin() as conn:
            existing=await self.t.journal._task(conn,lease.task_id)
            if existing['state']=='cancelled':
                return dict(task_id=lease.task_id,phase='cancelled',task_completed=True,completion=self._recorded(existing))
        await self.t.journal.renew(lease,ttl=60)
        async with media_mutation_lock,self.store.database.begin() as conn:
            task=await self._task(conn,lease);work=self._progress(task,lease);relations=await self.t.relations(task,conn=conn)
            grant=self.t._grant(task,relations,lease.epoch,lease.runner_id,'finalize')
            source=await self._source(conn,task,grant['payload'],work['source_record']) if work['source_record'] is not None else None
        if source is None:
            reply=await self._call(task,relations,grant,'source',agreement=None,source_census=None)
            if reply['phase']=='pending':
                result=await self.catalog.advance(lease)
                if result['master_catalog_verified'] is not True:return dict(task_id=lease.task_id,phase='catalog',task_completed=False)
                authority=await self.catalog.authorize(lease)
                grant=authority['grant']
                reply=await self._call(task,relations,grant,'source',agreement=authority['agreement'],source_census=authority['source_census'])
            witness=dict(version=1,agreement=reply['agreement'],source_census=reply['source_census'],receipt=reply['receipt'])
            async with media_mutation_lock,self.store.database.begin() as conn:
                current=await self._task(conn,lease)
                if self._progress(current,lease)!=work:raise TransferError('migration_cancel_finalize_progress_changed')
                await self._source(conn,current,grant['payload'],witness)
                await self._save(conn,current,{**work,'source_record':witness},lease)
            return dict(task_id=lease.task_id,phase='source_released',task_completed=False)
        if work['target_record'] is None:
            reply=await self._call(task,relations,grant,'target',source_record=work['source_record'])
            if reply['phase']=='released':
                if reply['source_record']!=work['source_record']:raise TransferError('migration_cancel_finalize_target_record_invalid')
                record=dict(preparation=reply['preparation'],receipt=reply['receipt'])
                self._target(record,grant['payload'],source,work['source_record'])
                async with media_mutation_lock,self.store.database.begin() as conn:
                    current=await self._task(conn,lease)
                    if self._progress(current,lease)!=work:raise TransferError('migration_cancel_finalize_progress_changed')
                    await self._source(conn,current,grant['payload'],work['source_record'])
                    await self._save(conn,current,{**work,'target_record':record},lease)
            return dict(task_id=lease.task_id,phase='target_'+reply['phase'],task_completed=False)
        self._target(work['target_record'],grant['payload'],source,work['source_record'])
        async with media_mutation_lock,self.store.database.begin() as conn:
            current=await self._task(conn,lease)
            if self._progress(current,lease)!=work:raise TransferError('migration_cancel_finalize_progress_changed')
            source=await self._source(conn,current,grant['payload'],work['source_record'])
            updated,more=await self._history(conn,current,lease,work)
            if more:
                await self._save(conn,current,updated,lease)
                return dict(task_id=lease.task_id,phase='history',task_completed=False)
            if (work['unit_count'],work['object_count'],work['total_bytes'])!=self._counts(current):
                raise TransferError('migration_cancel_finalize_history_incomplete')
            now=int(self.t.wall());runner=await self.t.journal._runner(conn,lease.task_id)
            completion=p.sign(self.store.unseal(self.store.node['private_key']),dict(format=FORMAT,version=1,
                **{name:current[name] for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship','plan_sha256')},
                units_sha256=current['plan']['units_sha256'],unit_count=current['plan']['unit_count'],object_count=current['object_count'],
                total_bytes=current['total_bytes'],committed_bytes=current['committed_bytes'],epoch=lease.epoch,runner_id=lease.runner_id,
                completed_at=now,census_sha256=work['census_sha256'],grant=grant,source_record=work['source_record'],target_record=work['target_record'],
                task_completed=True,source_files_removed=False,ownership_changed=False))
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==current['source_id']).values(
                used_bytes=source['retained_bytes'],updated_at=now))
            for table in (s.capacity,s.scopes):await conn.execute(delete(table).where(table.c.task_id==lease.task_id))
            await conn.execute(delete(s.drains).where(s.drains.c.member_id==current['source_id'],s.drains.c.owner_id==lease.task_id,s.drains.c.reason=='migration'))
            await conn.execute(delete(s.read_leases).where(s.read_leases.c.task_id==lease.task_id,s.read_leases.c.purpose=='migration'))
            await conn.execute(update(s.runners).where(s.runners.c.task_id==lease.task_id).values(expires_at=0))
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==lease.task_id).values(state='cancelled',revision=current['revision']+1,
                updated_at=now,error_code='',plan={**current['plan'],'phase':'cancelled',COMPLETION:completion,COMPLETION+'_sha256':digest(completion)}))
            await self.store.log(conn,'migration_cancelled',lease.runner_id,task_id=lease.task_id,completion_sha256=digest(completion),
                retained_bytes=source['retained_bytes'],committed_bytes=current['committed_bytes'])
            if not runner or (runner['runner_id'],runner['epoch'])!=(lease.runner_id,lease.epoch) or int(self.t.wall())>=runner['expires_at']:
                raise TransferError('migration_cancel_finalize_lease_expired')
        return dict(task_id=lease.task_id,phase='cancelled',task_completed=True,completion=completion)
