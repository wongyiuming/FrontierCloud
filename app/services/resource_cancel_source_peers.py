"""Retain uncommitted Source originals after signed Target shadow rollback.

Unit receipts do not settle task quota, release exclusions or authorize Source
deletion. Public mixed-task cancellation remains gated until final settlement.
"""
import stat

from sqlalchemy import select,update

from app.core.joined_io import joined_io
from app.services import resource_final_roots as roots,resource_migration_schema as s
from app.services.federation import protocol as p
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_authority import verify as verify_authorization
from app.services.resource_cancel_target import IDENTITIES,columns,decoded,verify_receipt as verify_target
from app.services.resource_observation import digest
from app.services.resource_source_verify import SourceVerification
from app.services.resource_transfer_protocol import WINDOW_SECONDS,TransferError,grant_shape,identifier

RECEIPT='frontiercloud-python-source-unit-retained'


def selected(unit,grant):
    parent=unit['parent_path']
    return dict(unit_id=unit['unit_id'],resource_kind=unit['resource_kind'],source_parent=parent,
        target_parent=parent if unit['resource_kind']=='media' else grant['target_relationship']+'/'+parent.split('/')[-1],
        unit_version=unit['source_version'],object_count=unit['object_count'],total_bytes=unit['total_bytes'])


def source_proof(proof,grant,unit,now):
    expected=dict(task_id=grant['task_id'],node_id=grant['source_id'],**{name:unit[name] for name in ('unit_id','unit_version','object_count','total_bytes')})
    try:
        if (not isinstance(proof,dict) or proof.keys()!={*expected,'root','directory','verified_at','expires_at'} or
                any(proof.get(name)!=value for name,value in expected.items()) or
                any(type(proof[name]) is not int for name in ('object_count','total_bytes','verified_at','expires_at')) or
                not 0<proof['verified_at']<=now+5 or not now<proof['expires_at']<=proof['verified_at']+WINDOW_SECONDS):raise ValueError()
        for name in ('root','directory'):
            item=proof[name]
            if (not isinstance(item,dict) or item.keys()!={'bytes','device','inode','mtime_ns','ctime_ns','mode','links'} or
                    any(type(value) is not int or not -2**127<value<2**127 for value in item.values()) or
                    any(item[field]<0 for field in ('bytes','device','inode','mode','links')) or
                    not item['inode'] or not item['links'] or not stat.S_ISDIR(item['mode'])):raise ValueError()
        return proof
    except (ValueError,KeyError,TypeError,OverflowError):raise TransferError('migration_cancel_retained_source_proof_invalid') from None


def verify_receipt(envelope,master_key,grant,unit,now,*,authorization=None,target_receipt=None,proof=None):
    try:
        if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:raise ValueError()
        value=p.verify(grant['source_key'],envelope)
        expected=dict(format=RECEIPT,version=1,node_id=grant['source_id'],**{name:grant[name] for name in IDENTITIES},
            **{name:unit[name] for name in ('unit_id','resource_kind','unit_version','object_count','total_bytes')},unit_sha256=digest(unit),
            source_files_removed=False,ownership_changed=False,capacity_settled=False)
        if (not isinstance(value,dict) or value.keys()!={*expected,'epoch','completed_at','authorization_sha256','target_receipt_sha256',
                'source_verification_sha256','source_roots_sha256'} or any(value.get(name)!=item for name,item in expected.items()) or
                any(type(value[name]) is not int for name in ('version','epoch','completed_at','object_count','total_bytes')) or
                any(type(value[name]) is not bool for name in ('source_files_removed','ownership_changed','capacity_settled')) or
                not 1<=value['epoch']<=grant['epoch'] or not 0<value['completed_at']<=now+5):raise ValueError()
        for name in ('authorization_sha256','target_receipt_sha256','source_verification_sha256','source_roots_sha256'):identifier(value[name],long=True)
        historical=dict(grant,epoch=value['epoch'])
        if target_receipt is not None:
            if digest(target_receipt)!=value['target_receipt_sha256']:raise ValueError()
            verify_target(target_receipt,grant['target_key'],master_key,historical,unit,value['completed_at'])
        if authorization is not None:
            if digest(authorization)!=value['authorization_sha256']:raise ValueError()
            verify_authorization(authorization,master_key,dict(historical,runner_id=authorization['payload']['runner_id']),unit,value['completed_at'])
        if proof is not None:
            if digest(proof)!=value['source_verification_sha256']:raise ValueError()
            source_proof(proof,historical,unit,value['completed_at'])
        return value
    except (p.ProtocolError,ValueError,KeyError,TypeError,RecursionError):raise TransferError('migration_cancel_source_receipt_invalid') from None


class RetainedVerification(SourceVerification):
    def __init__(self,service,deadline):
        super().__init__(service.t);self.service,self.deadline=service,deadline

    def _alive(self,value):
        super()._alive(value)
        if self.t.wall()>=self.deadline:raise TransferError('migration_cancel_authorization_expired')
        if self.t.traffic.snapshot().get('can_run') is not True:raise TransferError('migration_cancel_waiting_low_traffic')

    async def _unit(self,conn,grant,unit_id):
        _,unit,_=await self.service._unit(conn,grant,unit_id)
        if unit['state']=='cancelled':raise TransferError('migration_cancel_source_already_retained')
        return unit


class SourceCancellation:
    def __init__(self,transfers):self.t,self.store=transfers,transfers.store

    def _signed(self,node,relation,grant):
        value=grant_shape(p.verify(relation['peer_key'],grant),self.t.wall())
        if value['purpose']!='cancel' or value['source_id']!=node['node_id']:raise TransferError('migration_cancel_source_binding_invalid')
        return value

    async def _unit(self,conn,value,unit_id):
        header=await self.t._control_row(conn,value)
        unit=decoded(s.units,(await conn.execute(select(*columns(s.units)).where(s.units.c.task_id==value['task_id'],
            s.units.c.unit_id==unit_id).with_for_update())).mappings().first())
        if header['state']!='active' or header['side']!='source' or unit is None:raise TransferError('migration_cancel_source_unavailable')
        if (unit['state'] not in {'pending','copying','verified','cancelled'} or unit['committed_at'] or unit['cleanup_after'] or
                unit['publication'].get('commit') or unit['publication'].get('commit_sha256')):
            raise TransferError('migration_cancel_native_commit_is_final')
        self.t.native._unit(header,{('source_version' if name=='unit_version' else name):item for name,item in selected(unit,value).items()})
        scope=await conn.scalar(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==value['task_id'],s.scopes.c.member_id==value['source_id'],
            s.scopes.c.resource_kind==unit['resource_kind'],s.scopes.c.parent_path==unit['parent_path']))
        if 'cancel_settled' in unit['publication'] or unit['native_acknowledged']:
            from app.services.resource_cancel_unit_receipts import recorded
            recorded(unit,value,'source',self.t.wall())
            if scope:raise TransferError('migration_cancel_settlement_scope_changed')
        elif not scope:raise TransferError('migration_cancel_source_fence_missing')
        if not await conn.scalar(select(s.drains.c.owner_id).where(s.drains.c.member_id==value['source_id'],
                s.drains.c.owner_id==value['task_id'],s.drains.c.reason=='migration')):raise TransferError('migration_cancel_source_fence_missing')
        if await conn.scalar(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==value['task_id'],
                s.storage_journal.c.unit_id==unit_id).limit(1)):raise TransferError('migration_cancel_source_cleanup_unexpected')
        control=header['payload'].get('transfer',{})
        if control.get('source_cancel_stopped') is not True:raise TransferError('migration_cancel_source_stop_required')
        expected=control.get('source_roots')
        if not isinstance(expected,dict):raise TransferError('migration_cancel_source_root_unproven')
        return header,unit,expected

    async def _roots(self,expected):
        if not isinstance(expected,dict):raise TransferError('migration_cancel_source_root_unproven')
        try:await joined_io(roots.check,self.t.root_provider(),expected)
        except TransferError:raise TransferError('migration_cancel_source_root_changed') from None

    def _proof_root(self,unit,proof,expected):
        binding=expected['media' if unit['resource_kind']=='media' else 'recordings']
        if (binding.get('exists') is not True or
                any(proof['root'].get(name)!=binding.get(name) for name in ('device','inode','mode'))):
            raise TransferError('migration_cancel_source_root_changed')

    async def _readers(self,conn,value):
        return await conn.scalar(select(s.read_leases.c.lease_id).where(s.read_leases.c.member_id==value['source_id'],
            s.read_leases.c.task_id==value['task_id'],s.read_leases.c.purpose=='migration',s.read_leases.c.expires_at>self.t.wall()).limit(1))

    async def _close_scans(self,task_id):
        for name in ('source_verification','source_cleanup_scan','source_finalization_scan','source_cancel_census'):
            scan=getattr(self.t,name)
            if scan is not None and scan.key[0]==task_id:await joined_io(scan.close);setattr(self.t,name,None)

    def _reply(self,request_id,value,phase,unit=None,processed=0):
        return dict(format='frontiercloud-python-source-cancel-stop' if unit is None else 'frontiercloud-python-source-cancel-retain',
            version=1,request_id=request_id,node_id=value['source_id'],task_id=value['task_id'],epoch=value['epoch'],
            expires_at=value['expires_at'],phase=phase,**({} if unit is None else dict(unit_id=unit['unit_id'],unit_version=unit['source_version'],
                processed_objects=processed,receipt=unit['publication'].get('cancel_retained',{}).get('receipt'))),
            source_files_removed=False,ownership_changed=False,capacity_settled=False)

    async def stop(self,node,relation,request_id,grant):
        identifier(request_id);self._signed(node,relation,grant)
        value,_=await self.t._bind(grant,'source',expected='cancel',authenticated_relation=relation)
        if self.t.source_lock.locked():return self._reply(request_id,value,'waiting_writer')
        async with self.t.source_lock:
            await self._close_scans(value['task_id'])
            async with self.store.database.begin() as conn:
                header=await self.t._control_row(conn,value)
                if header['side']!='source' or header['state']!='active':raise TransferError('migration_cancel_source_unavailable')
                if await self._readers(conn,value):return self._reply(request_id,value,'waiting_reader')
                expected=header['payload'].get('transfer',{}).get('source_roots')
            await self._roots(expected)
            async with self.store.database.begin() as conn:
                current=await self.t._control_row(conn,value)
                if current['payload']!=header['payload']:raise TransferError('migration_cancel_source_changed')
                await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==value['task_id']).values(
                    payload={**current['payload'],'transfer':{**current['payload']['transfer'],'source_cancel_stopped':True}}))
                await self.store.log(conn,'migration_source_cancel_stopped',value['master_id'],task_id=value['task_id'])
        return self._reply(request_id,value,'stopped')

    async def _recorded(self,unit,value,master_key,expected):
        record=unit['publication'].get('cancel_retained')
        if (not isinstance(record,dict) or record.keys()!={'version','authorization','target_receipt','receipt'} or
                type(record['version']) is not int or record['version']!=1 or not isinstance(record['authorization'],dict) or
                not isinstance(record['target_receipt'],dict)):raise TransferError('migration_cancel_source_journal_changed')
        result=verify_receipt(record['receipt'],master_key,value,selected(unit,value),self.t.wall(),authorization=record['authorization'],
            target_receipt=record['target_receipt'],proof=unit['publication'].get('source_verification'))
        if (result['source_roots_sha256']!=digest(expected) or not isinstance(unit['publication'].get('source_verification'),dict)):
            raise TransferError('migration_cancel_source_journal_changed')
        self._proof_root(unit,unit['publication']['source_verification'],expected)
        await self._roots(expected)
        return record['receipt']

    async def advance(self,node,relation,request_id,grant,unit_id,authorization,target_receipt):
        identifier(request_id);identifier(unit_id,long=True);value=self._signed(node,relation,grant)
        value,active=await self.t._bind(grant,'source',expected='cancel',authenticated_relation=relation)
        if self.t.source_lock.locked():
            async with self.store.database.begin() as conn:_,unit,_=await self._unit(conn,value,unit_id)
            return self._reply(request_id,value,'waiting_writer',unit)
        async with self.t.source_lock:
            try:
                async with self.store.database.begin() as conn:_,unit,expected=await self._unit(conn,value,unit_id)
                if unit['state']=='cancelled':
                    await self._recorded(unit,value,relation['peer_key'],expected)
                    record=unit['publication']['cancel_retained']
                    if (authorization is not None and authorization!=record['authorization'] or
                            target_receipt is not None and target_receipt!=record['target_receipt']):
                        raise TransferError('migration_cancel_source_journal_changed')
                    return self._reply(request_id,value,'retained',unit,unit['object_count'])
                if authorization is None and target_receipt is None:
                    return self._reply(request_id,value,'pending',unit)
                chosen=selected(unit,value)
                permission=verify_authorization(authorization,relation['peer_key'],value,chosen,self.t.wall())
                verify_target(target_receipt,value['target_key'],relation['peer_key'],value,chosen,self.t.wall())
                await self._roots(expected)
                async with self.store.database.begin() as conn:
                    if await self._readers(conn,value):return self._reply(request_id,value,'waiting_reader',unit)
                if self.t.traffic.snapshot().get('can_run') is not True:return self._reply(request_id,value,'waiting_low_traffic',unit)
                verification=RetainedVerification(self,permission['expires_at'])
                result=await verification._step(node,active,request_id,value,unit_id)
                if result['phase']!='verified':return self._reply(request_id,value,result['phase'],unit,result['processed_objects'])
                await self._roots(expected);verification._alive(value)
                async with media_mutation_lock,self.store.database.begin() as conn:
                    _,current,actual=await self._unit(conn,value,unit_id);verification._alive(value)
                    if actual!=expected:raise TransferError('migration_cancel_source_root_changed')
                    proof=source_proof(current['publication'].get('source_verification'),value,chosen,self.t.wall())
                    if digest(proof)!=result['receipt_sha256']:raise TransferError('migration_cancel_source_journal_changed')
                    self._proof_root(current,proof,actual)
                    payload=dict(format=RECEIPT,version=1,node_id=value['source_id'],**{name:value[name] for name in IDENTITIES},
                        **{name:chosen[name] for name in ('unit_id','resource_kind','unit_version','object_count','total_bytes')},unit_sha256=digest(chosen),
                        epoch=value['epoch'],completed_at=int(self.t.wall()),authorization_sha256=digest(authorization),target_receipt_sha256=digest(target_receipt),
                        source_verification_sha256=digest(proof),source_roots_sha256=digest(expected),source_files_removed=False,ownership_changed=False,capacity_settled=False)
                    receipt=p.sign(self.store.unseal(node['private_key']),payload)
                    verify_receipt(receipt,relation['peer_key'],value,chosen,self.t.wall(),authorization=authorization,target_receipt=target_receipt,proof=proof)
                    retained=dict(version=1,authorization=authorization,target_receipt=target_receipt,receipt=receipt)
                    current={**current,'state':'cancelled','publication':{**current['publication'],'cancel_retained':retained}}
                    await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit_id).values(state='cancelled',publication=current['publication']))
                    await self.store.log(conn,'migration_source_unit_retained',value['master_id'],task_id=value['task_id'],unit_id=unit_id,receipt_sha256=digest(receipt))
                return self._reply(request_id,value,'retained',current,current['object_count'])
            except BaseException:
                if self.t.source_verification is not None:await joined_io(self.t.source_verification.close);self.t.source_verification=None
                raise
