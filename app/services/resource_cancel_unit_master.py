"""Atomic Master cancellation of one parent after both native settlements."""
import uuid

from sqlalchemy import delete,func,select,update

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_authority import CancellationAuthority
from app.services.resource_cancel_source_peers import verify_receipt as verify_source
from app.services.resource_cancel_target import IDENTITIES,verify_receipt as verify_target
from app.services.resource_cancel_unit_receipts import verify as verify_settled
from app.services.resource_cancel_terminal_receipts import master_unit
from app.services.resource_control_client import signed_request
from app.services.resource_migration_journal import Conflict,Lease
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError,identifier

FORMAT='frontiercloud-python-master-unit-cancelled'


class UnitCancellation:
    def __init__(self,store,transport,**options):
        self.authority=CancellationAuthority(store,transport,**options);self.cutover=self.authority.cutover
        self.t=self.authority.transfer;self.store,self.transport=store,transport

    @staticmethod
    def _selected(task,unit):
        parent=unit['parent_path']
        return dict(unit_id=unit['unit_id'],resource_kind=unit['resource_kind'],source_parent=parent,
            target_parent=parent if unit['resource_kind']=='media' else task['target_relationship']+'/'+parent.rsplit('/',1)[-1],
            unit_version=unit['source_version'],object_count=unit['object_count'],total_bytes=unit['total_bytes'])

    def _witnesses(self,source_receipt,target_receipt,grant,unit):
        master_key=p.public_key(self.store.unseal(self.store.node['private_key']))
        verify_target(target_receipt,grant['target_key'],master_key,grant,unit,self.t.wall())
        verify_source(source_receipt,master_key,grant,unit,self.t.wall(),target_receipt=target_receipt)

    async def _context(self,conn,lease,unit_id):
        task=await self.t.journal._fenced(conn,lease)
        if task['state']!='cancelling':raise TransferError('migration_cancel_request_required')
        unit=await self.cutover._unit(conn,task,unit_id)
        if unit['committed_at'] or unit['cleanup_after'] or unit['publication'].get('commit') or unit['state'] in {'committed','cleanup_pending','cleaned'}:
            raise TransferError('migration_cancel_master_commit_is_final')
        return task,unit

    def _work(self,unit):
        work=unit['publication'].get('cancel_unit_work')
        if (not isinstance(work,dict) or work.keys()!={'version','source_receipt','target_receipt','settlements'} or
                type(work['version']) is not int or work['version']!=1 or not isinstance(work['settlements'],dict) or
                set(work['settlements'])-{'source','target'} or any(not isinstance(work[name],dict) for name in ('source_receipt','target_receipt'))):
            raise TransferError('migration_cancel_master_journal_changed')
        return work

    async def _save(self,lease,unit,work):
        async with media_mutation_lock,self.store.database.begin() as conn:
            task,current=await self._context(conn,lease,unit['unit_id'])
            if current['publication']!=unit['publication'] or current['state']=='cancelled':raise TransferError('migration_cancel_master_journal_changed')
            await self.cutover._scopes(conn,task,current);await self.cutover._source_catalog(conn,task,current,require_copy=False)
            await conn.execute(update(s.units).where(s.units.c.task_id==lease.task_id,s.units.c.unit_id==unit['unit_id'])
                .values(publication={**current['publication'],'cancel_unit_work':work}))
            await self.store.log(conn,'migration_cancel_unit_observed',lease.runner_id,task_id=lease.task_id,unit_id=unit['unit_id'],peers=sorted(work['settlements']))

    async def _call(self,relation,grant,unit,side,work,authorization,*,query=False):
        request_id=uuid.uuid4().hex
        body=dict(request_id=request_id,grant=grant,unit_id=unit['unit_id'],authorization=None if query else authorization,
            source_receipt=None if query else work['source_receipt'],target_receipt=None if query else work['target_receipt'],
            source_settlement=None if query or side=='source' else work['settlements']['source'])
        response=await signed_request(self.store,relation,self.transport,'/internal/v1/migration/transfer/cancel-unit-settle',body,
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel')
        raw=grant['payload'];expected=dict(format='frontiercloud-python-cancel-unit-settlement',version=1,request_id=request_id,node_id=raw[side+'_id'],
            task_id=raw['task_id'],unit_id=unit['unit_id'],unit_version=unit['unit_version'],side=side,epoch=raw['epoch'],expires_at=raw['expires_at'],
            source_files_removed=False,ownership_changed=False,task_completed=False)
        if (not isinstance(response,dict) or response.keys()!={*expected,'phase','processed_objects','receipt'} or
                any(response.get(name)!=item for name,item in expected.items()) or
                any(type(response[name]) is not int for name in ('version','epoch','expires_at','processed_objects')) or
                any(type(response[name]) is not bool for name in ('source_files_removed','ownership_changed','task_completed')) or
                not 0<=response['processed_objects']<=unit['object_count'] or self.t.wall()>=response['expires_at'] or
                response['phase'] not in {'pending','objects','membership','waiting_writer','waiting_low_traffic','settled'} or
                response['phase']!='settled' and response['receipt'] is not None):raise TransferError('migration_cancel_settlement_peer_reply_invalid')
        if response['phase']=='settled':
            verify_settled(response['receipt'],raw,side,unit,self.t.wall(),source_receipt=work['source_receipt'],target_receipt=work['target_receipt'],
                source_settlement=None if side=='source' else work['settlements']['source'])
        return response

    def _recorded(self,task,unit):
        envelope=unit['publication'].get('cancel_unit_receipt')
        master_unit(envelope,p.public_key(self.store.unseal(self.store.node['private_key'])),task,unit,self.t.wall())
        return envelope

    async def _finish(self,lease,grant,selected,work):
        async with media_mutation_lock,self.store.database.begin() as conn:
            task,unit=await self._context(conn,lease,selected['unit_id'])
            if unit['state']=='cancelled':return lease,self._recorded(task,unit)
            if self._work(unit)!=work:raise TransferError('migration_cancel_master_journal_changed')
            self._witnesses(work['source_receipt'],work['target_receipt'],grant,selected)
            for side in ('source','target'):
                verify_settled(work['settlements'][side],grant,side,selected,self.t.wall(),source_receipt=work['source_receipt'],target_receipt=work['target_receipt'],
                    source_settlement=None if side=='source' else work['settlements']['source'])
            await self.cutover._scopes(conn,task,unit);await self.cutover._source_catalog(conn,task,unit,require_copy=False)
            committed=int(await conn.scalar(select(func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==lease.task_id,
                s.units.c.state.in_(['committed','cleanup_pending','cleaned']))))
            cancelled=int(await conn.scalar(select(func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==lease.task_id,s.units.c.state=='cancelled')))
            own=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==lease.task_id).with_for_update())).mappings().first()
            member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==task['target_id']).with_for_update())).mappings().one()
            expected=task['total_bytes']-committed-cancelled
            if (committed!=task['committed_bytes'] or not own or own['member_id']!=task['target_id'] or own['reserved_bytes']!=expected or
                    expected<unit['total_bytes'] or member['reserved_bytes']<expected or member['used_bytes']+member['reserved_bytes']>member['allocated_bytes']):
                raise TransferError('migration_cancel_master_capacity_inconsistent')
            amount=unit['total_bytes'];scopes=[]
            for side in ('source','target'):
                parent=selected[side+'_parent'];scope=digest([lease.task_id,task[side+'_id'],unit['resource_kind'],parent])
                matches=list((await conn.execute(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==lease.task_id,s.scopes.c.member_id==task[side+'_id'],
                    s.scopes.c.resource_kind==unit['resource_kind'],s.scopes.c.parent_path==parent).limit(2))).scalars())
                if matches!=[scope]:raise TransferError('migration_cancel_master_scope_changed')
                scopes.append(scope)
            payload=dict(format=FORMAT,version=1,node_id=task['master_id'],**{name:grant[name] for name in IDENTITIES},**selected,
                epoch=lease.epoch,revision=lease.revision+1,completed_at=int(self.t.wall()),source_receipt=work['source_receipt'],target_receipt=work['target_receipt'],
                settlements=work['settlements'],scope_released=True,source_drain_released=False,source_files_removed=False,ownership_changed=False,
                task_completed=False,capacity_refunded=amount)
            receipt=p.sign(self.store.unseal(self.store.node['private_key']),payload)
            changed=await conn.execute(update(s.objects).where(s.objects.c.task_id==lease.task_id,s.objects.c.unit_id==unit['unit_id'],s.objects.c.state.in_(['pending','copying','verified']))
                .values(state='cancelled',updated_at=int(self.t.wall())))
            if changed.rowcount!=unit['object_count']:raise TransferError('migration_cancel_master_objects_changed')
            await conn.execute(update(s.capacity).where(s.capacity.c.task_id==lease.task_id).values(reserved_bytes=expected-amount,updated_at=int(self.t.wall())))
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==task['target_id'])
                .values(reserved_bytes=member['reserved_bytes']-amount,updated_at=int(self.t.wall())))
            await conn.execute(delete(s.scopes).where(s.scopes.c.task_id==lease.task_id,s.scopes.c.scope_id.in_(scopes)))
            await conn.execute(update(s.units).where(s.units.c.task_id==lease.task_id,s.units.c.unit_id==unit['unit_id'])
                .values(state='cancelled',native_acknowledged=1,publication={**unit['publication'],'cancel_unit_receipt':receipt}))
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==lease.task_id).values(revision=lease.revision+1,updated_at=int(self.t.wall())))
            await self.store.log(conn,'migration_master_unit_cancelled',lease.runner_id,task_id=lease.task_id,unit_id=unit['unit_id'],refunded=amount,receipt_sha256=digest(receipt))
            return Lease(lease.task_id,lease.runner_id,lease.epoch,lease.revision+1),receipt

    async def advance(self,lease,unit_id,source_receipt=None,target_receipt=None):
        identifier(unit_id,long=True)
        async with self.store.database.begin() as conn:
            existing=await self.t.journal._task(conn,lease.task_id);unit=await self.cutover._unit(conn,existing,unit_id)
            if unit['state']=='cancelled':
                receipt=self._recorded(existing,unit)
                # A lost final reply must not strand the live runner on its
                # previous revision. Historical replay grants no new epoch or
                # expired lease; only the current live runner may continue.
                recovered=Lease(lease.task_id,lease.runner_id,lease.epoch,existing['revision'])
                try:await self.t.journal._fenced(conn,recovered)
                except Conflict:pass
                else:lease=recovered
                return lease,dict(phase='cancelled',unit_cancelled=True,receipt=receipt,task_completed=False)
        authorization,raw,chosen=await self.authority.issue(lease,unit_id)
        async with self.store.database.begin() as conn:
            task,unit=await self._context(conn,lease,unit_id);relations=await self.t.relations(task,conn=conn)
        work=unit['publication'].get('cancel_unit_work')
        if work is None:
            if 'cancel_unit_work' in unit['publication']:raise TransferError('migration_cancel_master_journal_changed')
            self._witnesses(source_receipt,target_receipt,raw,chosen)
            work=dict(version=1,source_receipt=source_receipt,target_receipt=target_receipt,settlements={})
            await self._save(lease,unit,work)
            return lease,dict(phase='witnesses',unit_cancelled=False,task_completed=False)
        work=self._work(unit);self._witnesses(work['source_receipt'],work['target_receipt'],raw,chosen)
        if (source_receipt is not None and source_receipt!=work['source_receipt'] or target_receipt is not None and target_receipt!=work['target_receipt']):
            raise TransferError('migration_cancel_master_journal_changed')
        grant=self.t._grant(task,relations,lease.epoch,lease.runner_id,'cancel')
        for side in ('source','target'):
            if side in work['settlements']:
                verify_settled(work['settlements'][side],raw,side,chosen,self.t.wall(),source_receipt=work['source_receipt'],target_receipt=work['target_receipt'],
                    source_settlement=None if side=='source' else work['settlements'].get('source'))
                continue
            result=await self._call(relations[task[side+'_id']],grant,chosen,side,work,authorization,query=True)
            if result['phase']=='pending':result=await self._call(relations[task[side+'_id']],grant,chosen,side,work,authorization)
            if result['phase']=='settled':
                work={**work,'settlements':{**work['settlements'],side:result['receipt']}}
                await self._save(lease,unit,work)
            return lease,dict(phase=side+'_'+result['phase'],unit_cancelled=False,task_completed=False)
        lease,receipt=await self._finish(lease,raw,chosen,work)
        return lease,dict(phase='cancelled',unit_cancelled=True,receipt=receipt,task_completed=False)
