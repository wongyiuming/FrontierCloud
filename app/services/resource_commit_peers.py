"""Idempotent native acknowledgement of a durable Master ownership transaction."""
from sqlalchemy import select,update

from app.services import resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,identifier

FIELDS={'format','version','task_id','master_id','source_id','target_id','source_relationship','target_relationship',
    'plan_sha256','units_sha256','epoch','unit_id','resource_kind','source_parent','target_parent','unit_version',
    'object_count','total_bytes','committed_at','cleanup_after','source_receipt_sha256','target_receipt_sha256'}


class CommitPeers:
    def __init__(self,transfers):self.t,self.store=transfers,transfers.store

    def _record(self,value,grant):
        if (not isinstance(value,dict) or value.keys()!=FIELDS or value['format']!='frontiercloud-python-resource-unit-commit' or
                type(value['version']) is not int or value['version']!=1 or
                any(type(value[name]) is not int for name in ('epoch','object_count','total_bytes','committed_at','cleanup_after')) or
                not 1<=value['epoch']<=grant['epoch'] or value['committed_at']>self.t.wall()+5 or
                value['committed_at']<1 or value['cleanup_after']-value['committed_at']!=3600 or
                any(value[name]!=grant[name] for name in ('task_id','master_id','source_id','target_id',
                    'source_relationship','target_relationship','plan_sha256','units_sha256'))):
            raise TransferError('migration_commit_record_invalid')
        for name in ('unit_id','unit_version','source_receipt_sha256','target_receipt_sha256'):identifier(value[name],long=True)
        return digest(value)

    async def acknowledge(self,node,relation,request_id,grant,commit):
        identifier(request_id)
        if self.t.source_lock.locked() or self.t.target_lock.locked():raise TransferError('migration_commit_writer_active')
        raw=grant.get('payload',{}) if isinstance(grant,dict) else {}
        side='source' if node['node_id']==raw.get('source_id') else 'target'
        async with (self.t.source_lock if side=='source' else self.t.target_lock):
            return await self._acknowledge(node,relation,request_id,grant,commit,side)

    async def _acknowledge(self,node,relation,request_id,grant,commit,side):
        value,_=await self.t._bind(grant,side,expected='commit',authenticated_relation=relation)
        checksum=self._record(commit,value)
        async with media_mutation_lock,self.store.database.begin() as conn:
            header=await self.t._control_row(conn,value)
            unit=(await conn.execute(select(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==commit['unit_id'])
                .with_for_update())).mappings().first()
            if not unit or header['state']!='active':raise TransferError('migration_commit_unit_unavailable')
            if unit['publication'].get('target_cancel'):
                raise TransferError('migration_commit_unit_cancelling')
            expected=dict(resource_kind=commit['resource_kind'],parent_path=commit[side+'_parent'],source_version=commit['unit_version'],
                object_count=commit['object_count'],total_bytes=commit['total_bytes'])
            if any(unit[name]!=item for name,item in expected.items()):raise TransferError('migration_commit_unit_changed')
            self.t.native._unit(header,dict(unit_id=commit['unit_id'],resource_kind=commit['resource_kind'],
                source_parent=commit['source_parent'],target_parent=commit['target_parent'],source_version=commit['unit_version'],
                object_count=commit['object_count'],total_bytes=commit['total_bytes']))
            if not await conn.scalar(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==value['task_id'],
                    s.scopes.c.member_id==node['node_id'],s.scopes.c.resource_kind==unit['resource_kind'],s.scopes.c.parent_path==unit['parent_path'])):
                raise TransferError('migration_commit_scope_missing')
            if unit['state'] in {'committed','cleanup_pending','cleaned'}:
                if unit['publication'].get('commit_sha256')!=checksum or unit['publication'].get('commit')!=commit:
                    raise TransferError('migration_commit_record_changed')
            else:
                if unit['state']!=('published' if side=='target' else 'verified'):
                    raise TransferError('migration_commit_native_proof_missing')
                if side=='target':
                    if unit['publication'].get('receipt_sha256')!=commit['target_receipt_sha256']:
                        raise TransferError('migration_commit_native_proof_changed')
                    member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==node['node_id']).with_for_update())).mappings().one()
                    capacity=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==value['task_id']).with_for_update())).mappings().one()
                    amount=unit['total_bytes']
                    if (capacity['reserved_bytes']<amount or member['reserved_bytes']<amount or
                            member['used_bytes']+member['reserved_bytes']>member['allocated_bytes']):
                        raise TransferError('migration_commit_native_capacity_inconsistent')
                    await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==node['node_id'])
                        .values(used_bytes=member['used_bytes']+amount,reserved_bytes=member['reserved_bytes']-amount,updated_at=int(self.t.wall())))
                    await conn.execute(update(s.capacity).where(s.capacity.c.task_id==value['task_id'])
                        .values(reserved_bytes=capacity['reserved_bytes']-amount,updated_at=int(self.t.wall())))
                else:
                    proof=unit['publication'].get('source_verification')
                    if (not proof or digest(proof)!=commit['source_receipt_sha256'] or
                            not proof['verified_at']-5<=commit['committed_at']<proof['expires_at']):
                        raise TransferError('migration_commit_native_proof_changed')
                await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit['unit_id'])
                    .values(state='committed',committed_at=commit['committed_at'],cleanup_after=commit['cleanup_after'],
                        publication={**unit['publication'],'commit':commit,'commit_sha256':checksum}))
                await self.store.log(conn,'migration_native_parent_committed',value['master_id'],task_id=value['task_id'],
                    unit_id=unit['unit_id'],side=side,bytes=unit['total_bytes'])
            await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==value['task_id'])
                .values(payload={**header['payload'],'transfer':{**header['payload']['transfer'],'commit_started':True}}))
            current=await self.t.native._row(conn,node,relation,value['task_id'])
            status=self.t.native._status(current,request_id)
        return dict(format='frontiercloud-python-resource-commit-ack',version=1,request_id=request_id,node_id=node['node_id'],
            task_id=value['task_id'],unit_id=commit['unit_id'],unit_version=commit['unit_version'],side=side,epoch=value['epoch'],
            commit_sha256=checksum,acknowledged=True,capacity_reserved=status['capacity_reserved'],
            committed_bytes=status['committed_bytes'],source_files_removed=False)
