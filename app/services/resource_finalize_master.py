"""Successful migration completion requires both native terminal receipts."""
import uuid

from sqlalchemy import delete,func,select,update

from app.services import karaoke_schema as ks,resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_cleanup_master import Cleanup
from app.services.resource_control_client import signed_request
from app.services.resource_final_receipts import CANCELLED_COMPLETION_FORMAT,verify as verify_receipt
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError


class Finalization:
    def __init__(self,store,transport,*,outcome='completed',**options):
        if outcome not in {'completed','cancelled'}:raise ValueError('Unknown terminal migration outcome')
        self.outcome=outcome
        self.cleanup=Cleanup(store,transport,**options);self.t=self.cleanup.t
        self.store,self.transport=store,transport

    @staticmethod
    def _counts(task):return task['plan']['unit_count'],task['object_count'],task['total_bytes']

    async def _task(self,conn,lease):
        task=await self.t.journal._fenced(conn,lease)
        if task['state']!=('cancelling' if self.outcome=='cancelled' else 'cleanup_pending') or task['committed_bytes']!=task['total_bytes']:
            raise TransferError('migration_finalize_cleanup_required')
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==task['task_id'],
            s.units.c.state=='cleaned',s.units.c.native_acknowledged==1))).one()
        objects=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.objects.c.expected_bytes),0)).where(
            s.objects.c.task_id==task['task_id'],s.objects.c.state=='cleaned'))).one()
        scopes=await conn.scalar(select(func.count()).select_from(s.scopes).where(s.scopes.c.task_id==task['task_id']))
        if (tuple(counts)!=self._counts(task) or tuple(objects)!=(task['object_count'],task['total_bytes']) or
                scopes!=task['plan']['unit_count']*2):raise TransferError('migration_finalize_work_incomplete')
        _,_,members=await self.cleanup.cutover._accounting(conn,task)
        if members['target']['used_bytes']<task['total_bytes']:raise TransferError('migration_finalize_target_usage_inconsistent')
        if (members['source']['used_bytes']!=0 or members['source']['reserved_bytes']!=0 or
                await conn.scalar(select(fs.global_media.c.media_id).where(fs.global_media.c.storage_member_id==task['source_id']).limit(1)) or
                await conn.scalar(select(ks.recordings.c.recording_id).where(ks.recordings.c.storage_member_id==task['source_id']).limit(1))):
            raise TransferError('migration_finalize_master_source_not_empty')
        return task

    def _progress(self,task,lease):
        previous=task['plan'].get('finalization',{})
        if (previous.get('epoch'),previous.get('runner_id'))==(lease.epoch,lease.runner_id):return previous
        return dict(epoch=lease.epoch,runner_id=lease.runner_id,after='',unit_count=0,object_count=0,total_bytes=0,
            census_sha256=digest(['master-final-census',1]),receipts=previous.get('receipts',{}))

    async def _save(self,conn,task,progress,lease):
        await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task['task_id']).values(
            plan={**task['plan'],'finalization':progress},updated_at=int(self.t.wall())))
        await self.store.log(conn,'migration_finalization_step',lease.runner_id,task_id=task['task_id'],
            catalog_units=progress['unit_count'],peers=sorted(progress['receipts']))

    async def advance(self,lease):
        async with self.store.database.begin() as conn:
            existing=await self.t.journal._task(conn,lease.task_id)
            if existing['state']==self.outcome:
                record=existing['plan'].get('completion')
                expected=CANCELLED_COMPLETION_FORMAT if self.outcome=='cancelled' else 'frontiercloud-python-migration-completion'
                if not record or record.get('format')!=expected or existing['plan'].get('completion_sha256')!=digest(record):
                    raise TransferError('migration_finalize_completion_record_changed')
                return dict(task_id=lease.task_id,phase=self.outcome,task_completed=True,completion=record)
        await self.t.journal.renew(lease,ttl=60)
        async with media_mutation_lock,self.store.database.begin() as conn:
            task=await self._task(conn,lease);progress=self._progress(task,lease)
            units=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id>progress['after'])
                .order_by(s.units.c.unit_id).limit(4))).mappings().all()
            for unit in units:
                await self.cleanup._task(conn,lease,unit['unit_id']);await self.cleanup._catalog(conn,task,unit)
                progress={**progress,'after':unit['unit_id'],'unit_count':progress['unit_count']+1,
                    'object_count':progress['object_count']+unit['object_count'],'total_bytes':progress['total_bytes']+unit['total_bytes'],
                    'census_sha256':digest([progress['census_sha256'],unit['unit_id'],unit['publication']['commit_sha256']])}
            if units:
                await self._save(conn,task,progress,lease)
                return dict(task_id=task['task_id'],phase='catalog',task_completed=False)
            if (progress['unit_count'],progress['object_count'],progress['total_bytes'])!=self._counts(task):
                raise TransferError('migration_finalize_master_census_incomplete')
        relations=await self.t.relations(task)
        grant=self.t._grant(task,relations,lease.epoch,lease.runner_id,'finalize');value=grant['payload']
        receipts=dict(progress['receipts'])
        for side in ('source','target'):
            if side in receipts:
                verify_receipt(receipts[side],value,side,self._counts(task),self.t.wall(),source_receipt=receipts.get('source'))
                continue
            request_id=uuid.uuid4().hex
            body=dict(request_id=request_id,grant=grant)
            if side=='target':body['source_receipt']=receipts['source']
            response=await signed_request(self.store,relations[task[side+'_id']],self.transport,
                '/internal/v1/migration/transfer/finalize-'+side,body,
                limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_finalize')
            expected=dict(format='frontiercloud-python-migration-finalization',version=1,request_id=request_id,
                node_id=task[side+'_id'],task_id=task['task_id'],side=side,epoch=lease.epoch,expires_at=value['expires_at'])
            if (not isinstance(response,dict) or response.keys()!={*expected,'phase','receipt'} or
                    any(response.get(name)!=item for name,item in expected.items()) or
                    any(type(response[name]) is not int for name in ('version','epoch','expires_at')) or
                    response['phase'] not in ('inventory','released') or self.t.wall()>=response['expires_at'] or
                    response['phase']=='inventory' and response['receipt'] is not None):
                raise TransferError('migration_finalize_peer_reply_invalid')
            if response['phase']=='released':
                verify_receipt(response['receipt'],value,side,self._counts(task),self.t.wall(),source_receipt=receipts.get('source'))
                receipts[side]=response['receipt']
            async with media_mutation_lock,self.store.database.begin() as conn:
                current=await self._task(conn,lease)
                if self._progress(current,lease)!=progress:raise TransferError('migration_finalize_progress_changed')
                progress={**progress,'receipts':receipts};await self._save(conn,current,progress,lease)
            return dict(task_id=task['task_id'],phase=side+'_'+response['phase'],task_completed=False)
        async with media_mutation_lock,self.store.database.begin() as conn:
            current=await self._task(conn,lease)
            if self._progress(current,lease)!=progress:raise TransferError('migration_finalize_progress_changed')
            for side in ('source','target'):
                verify_receipt(receipts[side],value,side,self._counts(current),self.t.wall(),source_receipt=receipts.get('source'))
            completion=dict(format=CANCELLED_COMPLETION_FORMAT if self.outcome=='cancelled' else 'frontiercloud-python-migration-completion',version=1,task_id=task['task_id'],
                plan_sha256=task['plan_sha256'],epoch=lease.epoch,completed_at=int(self.t.wall()),
                unit_count=progress['unit_count'],object_count=progress['object_count'],total_bytes=progress['total_bytes'],
                census_sha256=progress['census_sha256'],receipts=receipts)
            for table in (s.capacity,s.scopes):await conn.execute(delete(table).where(table.c.task_id==task['task_id']))
            await conn.execute(delete(s.drains).where(s.drains.c.member_id==task['source_id'],s.drains.c.owner_id==task['task_id'],s.drains.c.reason=='migration'))
            await conn.execute(update(s.runners).where(s.runners.c.task_id==task['task_id']).values(expires_at=0))
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task['task_id']).values(state=self.outcome,
                revision=current['revision']+1,updated_at=int(self.t.wall()),error_code='',
                plan={**current['plan'],**({'phase':'cancelled'} if self.outcome=='cancelled' else {}),
                    'finalization':progress,'completion':completion,'completion_sha256':digest(completion)}))
            await self.store.log(conn,'migration_cancelled_all_committed' if self.outcome=='cancelled' else 'migration_completed',
                lease.runner_id,task_id=task['task_id'],completion_sha256=digest(completion))
        return dict(task_id=task['task_id'],phase=self.outcome,task_completed=True,completion=completion)
