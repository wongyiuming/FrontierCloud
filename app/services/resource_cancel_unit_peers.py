"""Two-peer cancellation settlement for one uncommitted parent, without deletion."""
from sqlalchemy import delete,func,select,update

from app.core.joined_io import joined_io
from app.services import resource_cancel_files as files,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_authority import verify as verify_authorization
from app.services.resource_cancel_source_peers import SourceCancellation,RetainedVerification,selected as source_selected,source_proof,verify_receipt as verify_source
from app.services.resource_cancel_target import TargetCancellation,selected as target_selected,verify_receipt as verify_target
from app.services.resource_cancel_unit_receipts import FORMAT,IDENTITIES,recorded,verify as verify_settled
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,grant_shape,identifier


class SettlementVerification(RetainedVerification):
    async def _unit(self,conn,grant,unit_id):
        _,unit,_=await self.service._unit(conn,grant,unit_id)
        if unit['state']!='cancelled' or unit['native_acknowledged']:
            raise TransferError('migration_cancel_settlement_source_state_changed')
        return unit

    async def _persist(self,conn,value,current,proof):
        await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==current['unit_id']).values(
            publication={**current['publication'],'cancel_settlement_source_verification':proof}))


class UnitSettlement:
    def __init__(self,transfers):
        self.t,self.store=transfers,transfers.store
        self.source,self.target=SourceCancellation(transfers),TargetCancellation(transfers)

    async def _unit(self,conn,node,relation,value,unit_id,side):
        if side=='source':return await self.source._unit(conn,value,unit_id)
        unit,root=await self.target._unit(conn,node,relation,value,unit_id,control=True)
        return await self.t._control_row(conn,value),unit,root

    @staticmethod
    def _selected(unit,value,side):return (source_selected if side=='source' else target_selected)(unit,value)

    def _proofs(self,value,unit,side,master_key,source_receipt,target_receipt,source_settlement,now):
        chosen=self._selected(unit,value,side)
        verify_target(target_receipt,value['target_key'],master_key,value,chosen,now)
        source=verify_source(source_receipt,master_key,value,chosen,now,target_receipt=target_receipt)
        if side=='source':
            if source_settlement is not None or source_receipt!=unit['publication'].get('cancel_retained',{}).get('receipt'):
                raise TransferError('migration_cancel_settlement_peer_receipt_changed')
        else:
            if target_receipt!=unit['publication'].get('target_cancel',{}).get('receipt'):
                raise TransferError('migration_cancel_settlement_peer_receipt_changed')
            settled=verify_settled(source_settlement,value,'source',chosen,now,source_receipt=source_receipt,target_receipt=target_receipt)
            if settled['completed_at']<source['completed_at'] or settled['root_sha256']!=source['source_roots_sha256']:
                raise TransferError('migration_cancel_settlement_source_receipt_invalid')
        return chosen

    def _reply(self,request_id,value,side,unit,phase,processed=0):
        return dict(format='frontiercloud-python-cancel-unit-settlement',version=1,request_id=request_id,node_id=value[side+'_id'],
            task_id=value['task_id'],unit_id=unit['unit_id'],unit_version=unit['source_version'],side=side,epoch=value['epoch'],
            expires_at=value['expires_at'],phase=phase,processed_objects=processed,receipt=unit['publication'].get('cancel_settled',{}).get('receipt'),
            source_files_removed=False,ownership_changed=False,task_completed=False)

    async def _recorded(self,unit,value,side,master_key,root):
        record,result=recorded(unit,value,side,self.t.wall())
        historical=dict(value,epoch=result['epoch'],runner_id=record['authorization']['payload']['runner_id'])
        chosen=self._proofs(historical,unit,side,master_key,record['source_receipt'],record['target_receipt'],record['source_settlement'],result['completed_at'])
        verify_authorization(record['authorization'],master_key,historical,chosen,result['completed_at'])
        if result['root_sha256']!=digest(root):raise TransferError('migration_cancel_settlement_root_changed')
        if side=='source':
            proof=source_proof(unit['publication'].get('cancel_settlement_source_verification'),historical,chosen,result['completed_at'])
            if digest(proof)!=result['fresh_source_verification_sha256']:raise TransferError('migration_cancel_settlement_journal_changed')
            self.source._proof_root(unit,proof,root)
            await self.source._recorded(unit,value,master_key,root)
        else:
            if result['fresh_source_verification_sha256']!=record['source_settlement']['payload']['fresh_source_verification_sha256']:
                raise TransferError('migration_cancel_settlement_journal_changed')
            await self.target._recorded(unit,value,master_key,root)
        return record

    async def _capacity(self,conn,header,value,unit,side):
        member=dict((await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==value[side+'_id']).with_for_update())).mappings().one())
        own=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==value['task_id']).with_for_update())).mappings().first()
        amount=unit['total_bytes'] if side=='target' else 0
        if side=='source':
            if own and (own['member_id']!=value['source_id'] or own['reserved_bytes']):raise TransferError('migration_cancel_settlement_capacity_inconsistent')
            return member,0,0
        totals=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),func.coalesce(func.sum(s.units.c.total_bytes),0))
            .where(s.units.c.task_id==value['task_id']))).one()
        released=int(await conn.scalar(select(func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==value['task_id'],
            (s.units.c.committed_at>0)|((s.units.c.state=='cancelled')&(s.units.c.native_acknowledged==1)))))
        expected=header['expected_bytes']-released
        if (tuple(totals)!=(header['expected_units'],header['expected_objects'],header['expected_bytes']) or not own or
                own['member_id']!=value['target_id'] or own['reserved_bytes']!=expected or expected<amount or
                member['reserved_bytes']<expected or member['used_bytes']+member['reserved_bytes']>member['allocated_bytes']):
            raise TransferError('migration_cancel_settlement_capacity_inconsistent')
        return member,expected,amount

    async def advance(self,node,relation,request_id,grant,unit_id,authorization,source_receipt,target_receipt,source_settlement):
        identifier(request_id);identifier(unit_id,long=True)
        value=grant_shape(p.verify(relation['peer_key'],grant),self.t.wall())
        side='source' if node['node_id']==value['source_id'] else 'target' if node['node_id']==value['target_id'] else ''
        if value['purpose']!='cancel' or not side:raise TransferError('migration_cancel_settlement_binding_invalid')
        value,active=await self.t._bind(grant,side,expected='cancel',authenticated_relation=relation)
        lock=self.t.source_lock if side=='source' else self.t.target_lock
        if lock.locked():
            async with self.store.database.begin() as conn:_,unit,_=await self._unit(conn,node,relation,value,unit_id,side)
            return self._reply(request_id,value,side,unit,'waiting_writer')
        async with lock:
            try:
                async with self.store.database.begin() as conn:header,unit,root=await self._unit(conn,node,relation,value,unit_id,side)
                if unit['publication'].get('cancel_settled') is not None:
                    record=await self._recorded(unit,value,side,relation['peer_key'],root)
                    for name,item in (('authorization',authorization),('source_receipt',source_receipt),('target_receipt',target_receipt),('source_settlement',source_settlement)):
                        if item is not None and item!=record[name]:raise TransferError('migration_cancel_settlement_journal_changed')
                    return self._reply(request_id,value,side,unit,'settled',unit['object_count'])
                if unit['state']!='cancelled' or unit['native_acknowledged']:raise TransferError('migration_cancel_settlement_unit_not_cancelled')
                if all(item is None for item in (authorization,source_receipt,target_receipt,source_settlement)):
                    return self._reply(request_id,value,side,unit,'pending')
                chosen=self._selected(unit,value,side)
                permission=verify_authorization(authorization,relation['peer_key'],value,chosen,self.t.wall())
                self._proofs(value,unit,side,relation['peer_key'],source_receipt,target_receipt,source_settlement,self.t.wall())
                if side=='source':
                    await self.source._recorded(unit,value,relation['peer_key'],root)
                    if self.t.traffic.snapshot().get('can_run') is not True:return self._reply(request_id,value,side,unit,'waiting_low_traffic')
                    verification=SettlementVerification(self.source,permission['expires_at'])
                    result=await verification._step(node,active,request_id,value,unit_id)
                    if result['phase']!='verified':return self._reply(request_id,value,side,unit,result['phase'],result['processed_objects'])
                    await self.source._roots(root);verification._alive(value)
                    fresh_sha=result['receipt_sha256']
                else:
                    await self.target._recorded(unit,value,relation['peer_key'],root)
                    async with self.store.database.begin() as conn:
                        first=await conn.scalar(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==value['task_id'],
                            s.storage_journal.c.unit_id==unit_id).order_by(s.storage_journal.c.logical_id).limit(1))
                        _,obj=await self.target._object(conn,value,unit,first)
                    physical=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
                    await joined_io(files.verify_removed,physical,value['task_id'],unit,obj,root)
                    fresh_sha=source_settlement['payload']['fresh_source_verification_sha256']
                async with media_mutation_lock,self.store.database.begin() as conn:
                    header,current,actual=await self._unit(conn,node,relation,value,unit_id,side)
                    self.t._alive(value);verify_authorization(authorization,relation['peer_key'],value,chosen,self.t.wall())
                    if actual!=root or current['native_acknowledged'] or current['publication'].get('cancel_settled') is not None:
                        raise TransferError('migration_cancel_settlement_journal_changed')
                    if side=='source':
                        proof=source_proof(current['publication'].get('cancel_settlement_source_verification'),value,chosen,self.t.wall())
                        self.source._proof_root(current,proof,root)
                        if digest(proof)!=fresh_sha:raise TransferError('migration_cancel_settlement_journal_changed')
                    scopes=list((await conn.execute(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==value['task_id'],s.scopes.c.member_id==value[side+'_id'],
                        s.scopes.c.resource_kind==current['resource_kind'],s.scopes.c.parent_path==current['parent_path']).limit(2).with_for_update())).scalars())
                    if scopes!=[digest([current['resource_kind'],value[side+'_id'],current['parent_path']])]:raise TransferError('migration_cancel_settlement_scope_changed')
                    member,reserved,amount=await self._capacity(conn,header,value,current,side)
                    payload=dict(format=FORMAT,version=1,node_id=value[side+'_id'],side=side,**{name:value[name] for name in IDENTITIES},
                        **{name:chosen[name] for name in ('unit_id','resource_kind','unit_version','object_count','total_bytes')},unit_sha256=digest(chosen),
                        epoch=value['epoch'],completed_at=int(self.t.wall()),scope_id=scopes[0],root_sha256=digest(root),
                        authorization_sha256=digest(authorization),source_receipt_sha256=digest(source_receipt),target_receipt_sha256=digest(target_receipt),
                        source_settlement_sha256='' if side=='source' else digest(source_settlement),fresh_source_verification_sha256=fresh_sha,
                        capacity_refunded=amount,task_reserved_before=reserved,task_reserved_after=reserved-amount,
                        member_reserved_before=member['reserved_bytes'],member_reserved_after=member['reserved_bytes']-amount,
                        used_before=member['used_bytes'],used_after=member['used_bytes'],scope_released=True,source_files_removed=False,ownership_changed=False,task_completed=False)
                    receipt=p.sign(self.store.unseal(node['private_key']),payload)
                    verify_settled(receipt,value,side,chosen,self.t.wall(),authorization=authorization,source_receipt=source_receipt,
                        target_receipt=target_receipt,source_settlement=source_settlement)
                    if amount:
                        await conn.execute(update(s.capacity).where(s.capacity.c.task_id==value['task_id']).values(reserved_bytes=reserved-amount,updated_at=int(self.t.wall())))
                        await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==value[side+'_id'])
                            .values(reserved_bytes=member['reserved_bytes']-amount,updated_at=int(self.t.wall())))
                    await conn.execute(delete(s.scopes).where(s.scopes.c.scope_id==scopes[0],s.scopes.c.task_id==value['task_id']))
                    record=dict(version=1,authorization=authorization,source_receipt=source_receipt,target_receipt=target_receipt,
                        source_settlement=source_settlement,receipt=receipt)
                    current={**current,'native_acknowledged':1,'publication':{**current['publication'],'cancel_settled':record}}
                    await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit_id)
                        .values(native_acknowledged=1,publication=current['publication']))
                    await self.store.log(conn,'migration_native_unit_cancel_settled',value['master_id'],task_id=value['task_id'],unit_id=unit_id,
                        side=side,refunded=amount,receipt_sha256=digest(receipt))
                return self._reply(request_id,value,side,current,'settled',current['object_count'])
            except BaseException:
                if side=='source' and self.t.source_verification is not None:
                    await joined_io(self.t.source_verification.close);self.t.source_verification=None
                raise
