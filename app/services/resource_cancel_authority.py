"""Short Master permission for cancelling one genuinely uncommitted parent.

This permission is not proof of retained native Source bytes. Native cancellation
must additionally obtain and verify that Source proof before removing a Target
shadow. It cannot be used as a commit, a Source deletion grant or a quota refund.
"""
from sqlalchemy import select

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_cutover_master import COMMITTED,Cutover
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import WINDOW_SECONDS,TransferError,identifier

FORMAT='frontiercloud-python-unit-cancel-authorization'
IDENTITIES=('task_id','master_id','source_id','target_id','source_relationship','target_relationship',
    'plan_sha256','units_sha256','source_binding','target_binding','source_key','target_key','epoch','runner_id')
UNIT_FIELDS=('unit_id','resource_kind','source_parent','target_parent','unit_version','object_count','total_bytes')


def verify(envelope,master_key,grant,unit,now):
    if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:
        raise TransferError('migration_cancel_authorization_invalid')
    try:value=p.verify(master_key,envelope)
    except p.ProtocolError:raise TransferError('migration_cancel_authorization_invalid') from None
    expected=dict(format=FORMAT,version=1,**{name:grant[name] for name in IDENTITIES},
        **{name:unit[name] for name in UNIT_FIELDS},source_owns_unit=True,ownership_rollback=False)
    if (not isinstance(value,dict) or value.keys()!={*expected,'revision','created_at','expires_at'} or
            any(value.get(name)!=item for name,item in expected.items()) or
            any(type(value[name]) is not int for name in ('version','epoch','revision','object_count','total_bytes','created_at','expires_at')) or
            value['revision']<1 or value['epoch']<1 or value['object_count']<1 or value['total_bytes']<1 or
            type(value['source_owns_unit']) is not bool or type(value['ownership_rollback']) is not bool or
            value['expires_at']-value['created_at']!=WINDOW_SECONDS or now<value['created_at']-5):
        raise TransferError('migration_cancel_authorization_invalid')
    if now>=value['expires_at']:raise TransferError('migration_cancel_authorization_expired')
    return value


class CancellationAuthority:
    def __init__(self,store,transport,**options):
        self.cutover=Cutover(store,transport,**options);self.transfer=self.cutover.transfer
        self.store=store

    async def issue(self,lease,unit_id):
        identifier(unit_id,long=True)
        await self.transfer.journal.renew(lease,ttl=60)
        async with media_mutation_lock,self.store.database.begin() as conn:
            task=await self.transfer.journal._fenced(conn,lease)
            if task['state']!='cancelling':raise TransferError('migration_cancel_request_required')
            unit=await self.cutover._unit(conn,task,unit_id)
            if (unit['state'] in COMMITTED or unit['state']=='cancelled' or unit['committed_at'] or
                    unit['cleanup_after'] or unit['publication'].get('commit') or unit['publication'].get('commit_sha256')):
                raise TransferError('migration_cancel_master_commit_is_final')
            target_parent=await self.cutover._scopes(conn,task,unit)
            await self.cutover._source_catalog(conn,task,unit,require_copy=False)
            own=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==task['task_id']).with_for_update())).mappings().first()
            member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==task['target_id']).with_for_update())).mappings().first()
            if (not own or own['member_id']!=task['target_id'] or own['reserved_bytes']<unit['total_bytes'] or
                    not member or member['reserved_bytes']<own['reserved_bytes']):
                raise TransferError('migration_cancel_capacity_inconsistent')
            relations=await self.transfer.relations(task,conn=conn)
            grant=self.transfer._grant(task,relations,lease.epoch,lease.runner_id,'cancel')['payload']
            selected=dict(unit_id=unit_id,resource_kind=unit['resource_kind'],source_parent=unit['parent_path'],
                target_parent=target_parent,unit_version=unit['source_version'],object_count=unit['object_count'],total_bytes=unit['total_bytes'])
            now=int(self.transfer.wall())
            value=dict(format=FORMAT,version=1,**{name:grant[name] for name in IDENTITIES},**selected,
                revision=lease.revision,created_at=now,expires_at=now+WINDOW_SECONDS,source_owns_unit=True,ownership_rollback=False)
            envelope=p.sign(self.store.unseal(self.store.node['private_key']),value)
            verify(envelope,p.public_key(self.store.unseal(self.store.node['private_key'])),grant,selected,self.transfer.wall())
            await self.store.log(conn,'migration_cancel_unit_authorized',lease.runner_id,
                task_id=task['task_id'],unit_id=unit_id,epoch=lease.epoch,authorization_sha256=digest(envelope))
            return envelope,grant,selected
