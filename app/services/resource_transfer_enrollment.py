"""Bounded, replayable enrollment of all admitted metadata before the first copy."""
import uuid

from sqlalchemy import func,select,update

from app.services import resource_migration_schema as s
from app.services.resource_control_client import signed_request
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError


class Enrollment:
    def __init__(self,transfer):self.t=transfer

    async def advance(self,lease):
        await self.t.journal.renew(lease,ttl=60)
        async with self.t.store.database.begin() as conn:
            task=await self.t.journal._fenced(conn,lease)
            if task['state']!='copying':raise TransferError('migration_metadata_state_changed')
            execution=task['plan'].get('execution',{})
            if execution.get('writable_verified') is not True or execution.get('peer_verified') is not True:
                raise TransferError('migration_transfer_preflight_required')
            progress=execution.get('metadata',dict(version=1,after='',objects=0,bytes=0,complete=False))
            if (not isinstance(progress,dict) or progress.keys()!={'version','after','objects','bytes','complete'} or
                    type(progress['version']) is not int or progress['version']!=1 or not isinstance(progress['after'],str) or
                    any(type(progress[name]) is not int or progress[name]<0 for name in ('objects','bytes')) or
                    type(progress['complete']) is not bool):raise TransferError('migration_metadata_journal_changed')
            if progress['complete']:return True
            row=(await conn.execute(select(s.plan_bindings.c.logical_id).where(s.plan_bindings.c.plan_id==task['task_id'],
                s.plan_bindings.c.logical_id>progress['after']).order_by(s.plan_bindings.c.logical_id).limit(1))).first()
            if row is None:
                counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.objects.c.expected_bytes),0))
                    .where(s.objects.c.task_id==task['task_id']))).one()
                if tuple(counts)!=(task['object_count'],task['total_bytes']) or tuple(counts)!=(progress['objects'],progress['bytes']):
                    raise TransferError('migration_metadata_roster_changed')
                await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task['task_id']).values(
                    plan={**task['plan'],'execution':{**execution,'metadata':{**progress,'complete':True}}}))
                return True
            obj=await self.t._object(conn,task,row.logical_id)
            relations=await self.t.relations(task,conn=conn)
            grant=self.t._grant(task,relations,lease.epoch,lease.runner_id,'register',obj)
        for side in ('source','target'):
            request_id=uuid.uuid4().hex
            value=await signed_request(self.t.store,relations[task[side+'_id']],self.t.transport,
                '/internal/v1/migration/transfer/register',dict(request_id=request_id,grant=grant),limit=CONTROL_BYTES,
                error_type=TransferError,prefix='migration_metadata')
            expected=dict(format='frontiercloud-python-transfer-metadata',version=1,request_id=request_id,node_id=task[side+'_id'],
                task_id=task['task_id'],logical_id=obj['logical_id'],epoch=lease.epoch,grant_sha256=digest(grant),
                registered=True,expires_at=grant['payload']['expires_at'])
            if (not isinstance(value,dict) or value!=expected or type(value.get('version')) is not int or
                    type(value.get('epoch')) is not int or type(value.get('expires_at')) is not int or value.get('registered') is not True or
                    self.t.wall()>=value['expires_at']):raise TransferError('migration_metadata_reply_invalid')
        async with self.t.store.database.begin() as conn:
            current=await self.t.journal._fenced(conn,lease)
            if current['plan'].get('execution',{})!=execution:raise TransferError('migration_metadata_journal_changed')
            updated={**progress,'after':obj['logical_id'],'objects':progress['objects']+1,'bytes':progress['bytes']+obj['expected_bytes']}
            if updated['objects']>task['object_count'] or updated['bytes']>task['total_bytes']:
                raise TransferError('migration_metadata_roster_changed')
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task['task_id']).values(
                plan={**current['plan'],'execution':{**execution,'metadata':updated}},updated_at=int(self.t.wall())))
        return False
