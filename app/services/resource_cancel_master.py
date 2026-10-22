"""Restartable cancellation of copied parents; task finalization is separate."""
from sqlalchemy import func,select,update

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_client import CancellationClient
from app.services.resource_cancel_source import CancellationSource,verify as verify_proof
from app.services.resource_cancel_unit_master import UnitCancellation
from app.services.resource_cutover_master import COMMITTED
from app.services.resource_transfer_protocol import TransferError


class PublicationCancellation:
    def __init__(self,store,transport,**options):
        self.units=UnitCancellation(store,transport,**options);self.t,self.store=self.units.t,store
        self.source=CancellationSource(store,transport,**options);self.client=CancellationClient(self.t)

    async def _context(self,conn,lease):
        task=await self.t.journal._fenced(conn,lease)
        if task['state']!='cancelling':raise TransferError('migration_cancel_request_required')
        relations=await self.t.relations(task,conn=conn)
        if 'cancel_native_stops' in task['plan']:
            journal=task['plan']['cancel_native_stops']
            if journal is None:raise TransferError('migration_cancel_stop_journal_changed')
            # Only this coordinator persists the first signed stop record,
            # after proving the complete admitted metadata roster. Its grant binds all
            # immutable task counts and hashes through both reservations.
            # Later steps validate that proof and their selected parent rather
            # than scanning every task object again for each metadata page.
            self.client.stopped(journal,task,relations,lease)
            return task,relations
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),func.coalesce(func.sum(s.units.c.total_bytes),0))
            .where(s.units.c.task_id==lease.task_id))).one()
        from app.services.resource_cancel_roster import validate
        await validate(conn,task)
        if tuple(counts)!=(task['plan']['unit_count'],task['object_count'],task['total_bytes']):
            raise TransferError('migration_cancel_metadata_roster_required')
        return task,relations

    @staticmethod
    def _progress(work,side,value):
        fields=('phase','processed_objects','deleted_count','deleted_bytes') if side=='target' else ('phase','processed_objects')
        return {**work,'progress':{**work['progress'],side:{name:value[name] for name in fields}}}

    def _work(self,unit):
        if 'cancel_native_work' not in unit['publication']:
            return dict(version=1,target_receipt=None,source_receipt=None,progress={})
        work=unit['publication']['cancel_native_work']
        if (not isinstance(work,dict) or work.keys()!={'version','target_receipt','source_receipt','progress'} or
                type(work['version']) is not int or work['version']!=1 or not isinstance(work['progress'],dict) or
                set(work['progress'])-{'source_proof','target','source'} or
                any(work[name] is not None and not isinstance(work[name],dict) for name in ('source_receipt','target_receipt')) or
                work['source_receipt'] is not None and work['target_receipt'] is None):raise TransferError('migration_cancel_native_journal_changed')
        for side,value in work['progress'].items():
            fields={'phase','processed_objects','deleted_count','deleted_bytes'} if side=='target' else {'phase','processed_objects'}
            phases=({'roster','objects','directory','receipt','cancelled','waiting_writer','waiting_reader','waiting_low_traffic'} if side=='target' else
                {'objects','membership','verified'} if side=='source_proof' else {'pending','objects','membership','retained','waiting_writer','waiting_reader','waiting_low_traffic'})
            if (not isinstance(value,dict) or value.keys()!=fields or not isinstance(value['phase'],str) or value['phase'] not in phases or
                    any(type(value[name]) is not int or not 0<=value[name]<= (unit['total_bytes'] if name=='deleted_bytes' else unit['object_count']) for name in fields-{'phase'})):
                raise TransferError('migration_cancel_native_journal_changed')
        return work

    async def _save(self,lease,unit,work):
        async with media_mutation_lock,self.store.database.begin() as conn:
            task,_=await self._context(conn,lease);current=await self.units.cutover._unit(conn,task,unit['unit_id'])
            if current['publication']!=unit['publication'] or current['state']!=unit['state']:
                raise TransferError('migration_cancel_native_journal_changed')
            await self.units.cutover._scopes(conn,task,current);await self.units.cutover._source_catalog(conn,task,current,require_copy=False)
            await conn.execute(update(s.units).where(s.units.c.task_id==lease.task_id,s.units.c.unit_id==unit['unit_id'])
                .values(publication={**current['publication'],'cancel_native_work':work}))
            await self.store.log(conn,'migration_cancel_native_observed',lease.runner_id,task_id=lease.task_id,unit_id=unit['unit_id'],
                target_removed=work['target_receipt'] is not None,source_retained=work['source_receipt'] is not None,progress=work['progress'])

    async def _stops(self,lease,task,relations):
        old=task['plan'].get('cancel_native_stops')
        if old is None and 'cancel_native_stops' in task['plan']:raise TransferError('migration_cancel_stop_journal_changed')
        journal=self.client.stopped(old,task,relations,lease)
        for side in ('source','target'):
            record=journal['receipts'].get(side)
            if record is not None and record['reply']['payload']['phase']=='stopped':continue
            record,value=await self.client.stop(task,relations,lease,side)
            journal={**journal,'receipts':{**journal['receipts'],side:record}}
            async with media_mutation_lock,self.store.database.begin() as conn:
                current,_=await self._context(conn,lease)
                if current['plan']!=task['plan']:raise TransferError('migration_cancel_stop_journal_changed')
                await conn.execute(update(s.tasks).where(s.tasks.c.task_id==lease.task_id).values(plan={**current['plan'],'cancel_native_stops':journal}))
                await self.store.log(conn,'migration_cancel_native_stop_observed',lease.runner_id,task_id=lease.task_id,side=side,phase=value['phase'])
            return side+'_'+value['phase']
        return None

    def _proof(self,unit,grant,selected):
        proof=unit['publication'].get('cancel_source')
        try:
            if not isinstance(proof,dict) or proof.keys()!={'authorization','source_reply'}:return None
            verify_proof(proof['source_reply'],p.public_key(self.store.unseal(self.store.node['private_key'])),
                proof['authorization'],grant,selected,self.t.wall()+5)
            return proof
        except (p.ProtocolError,KeyError,TypeError,RecursionError):return None

    async def advance(self,lease):
        await self.t.journal.renew(lease,ttl=60)
        async with self.store.database.begin() as conn:task,relations=await self._context(conn,lease)
        phase=await self._stops(lease,task,relations)
        if phase:return lease,dict(phase=phase,units_settled=False,task_completed=False)
        async with self.store.database.begin() as conn:
            task,relations=await self._context(conn,lease)
            unack=await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==lease.task_id,s.units.c.state.in_(COMMITTED),
                s.units.c.native_acknowledged==0).order_by(s.units.c.unit_id).limit(1))
            row=(await conn.execute(select(s.units).where(s.units.c.task_id==lease.task_id,s.units.c.state.not_in((*COMMITTED,'cancelled')))
                .order_by(s.units.c.unit_id).limit(1))).mappings().first()
        if unack:
            result=await self.units.cutover.acknowledge_one(lease,unack)
            return lease,dict(phase='committed_acknowledgement',unit_id=unack,native_acknowledged=result['native_acknowledged'],units_settled=False,task_completed=False)
        if row is None:return lease,dict(phase='units_settled',units_settled=True,task_completed=False)
        unit=dict(row);work=self._work(unit)
        if work['source_receipt'] is not None:
            lease,result=await self.units.advance(lease,unit['unit_id'],work['source_receipt'],work['target_receipt'])
            return lease,dict(phase='settlement_'+result['phase'],unit_id=unit['unit_id'],units_settled=False,task_completed=False)
        authorization,raw,selected=await self.units.authority.issue(lease,unit['unit_id'])
        if work['target_receipt'] is None:
            proof=self._proof(unit,raw,selected)
            if proof is None:
                result=await self.source.advance(lease,unit['unit_id'])
                async with self.store.database.begin() as conn:
                    current,_=await self._context(conn,lease);unit=await self.units.cutover._unit(conn,current,unit['unit_id'])
                work=self._progress(work,'source_proof',result);await self._save(lease,unit,work)
                return lease,dict(phase='source_proof_'+result['phase'],unit_id=unit['unit_id'],units_settled=False,task_completed=False)
            result,raw=await self.client.target(task,relations,lease,selected,proof)
            work=self._progress(work,'target',result)
            if result['phase']=='cancelled':work={**work,'target_receipt':result['receipt']}
            await self._save(lease,unit,work)
            return lease,dict(phase='target_'+result['phase'],unit_id=unit['unit_id'],units_settled=False,task_completed=False)
        master_key=p.public_key(self.store.unseal(self.store.node['private_key']))
        from app.services.resource_cancel_target import verify_receipt as verify_target
        verify_target(work['target_receipt'],raw['target_key'],master_key,raw,selected,self.t.wall())
        result,_=await self.client.source(task,relations,lease,selected,None,work['target_receipt'],query=True)
        if result['phase']=='pending':result,_=await self.client.source(task,relations,lease,selected,authorization,work['target_receipt'])
        work=self._progress(work,'source',result)
        if result['phase']=='retained':work={**work,'source_receipt':result['receipt']}
        await self._save(lease,unit,work)
        return lease,dict(phase='source_'+result['phase'],unit_id=unit['unit_id'],units_settled=False,task_completed=False)
