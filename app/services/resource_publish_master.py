"""Drive bounded Target publication; global ownership still requires cutover."""
import json
import uuid

from sqlalchemy import func,select,update

from app.services import resource_migration_schema as s
from app.services.resource_control_client import signed_request
from app.services.resource_transfer_master import TransferMaster
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError,identifier
from app.services import resource_migration_pipeline as pipeline


class Publication:
    def __init__(self,store,transport,**options):
        self.transfer=TransferMaster(store,transport,**options)
        self.store,self.transport=store,transport

    async def enter(self,lease):
        async with self.store.database.begin() as conn:
            task=await self.transfer.journal._fenced(conn,lease)
            if task['state']=='committing':return lease
            await pipeline.verified(conn,task,await pipeline.current(conn,task))
        if task['state']=='committing':return lease
        target={'copying':'verifying','verifying':'committing'}.get(task['state'])
        if target is None:raise TransferError('migration_publish_state_changed')
        return await self.transfer.journal.checkpoint(lease,target,committed_bytes=task['committed_bytes'])

    async def _call(self,task,relations,lease,unit,action,**values):
        request_id=uuid.uuid4().hex
        grant=self.transfer._grant(task,relations,lease.epoch,lease.runner_id,'publish')
        response=await signed_request(self.store,relations[task['target_id']],self.transport,
            '/internal/v1/migration/transfer/publish-'+action,dict(request_id=request_id,grant=grant,**values),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_publish')
        expected=dict(format='frontiercloud-python-target-publication',version=1,request_id=request_id,
            node_id=task['target_id'],task_id=task['task_id'],unit_id=unit['unit_id'],unit_version=unit['source_version'],
            object_count=unit['object_count'],total_bytes=unit['total_bytes'],epoch=lease.epoch,capacity_settled=False)
        expected['expires_at']=grant['payload']['expires_at']
        if (not isinstance(response,dict) or response.keys()!={*expected,'state','phase','processed','receipt_sha256'} or
            any(response.get(key)!=value for key,value in expected.items()) or
            any(type(response[key]) is not int for key in ('version','object_count','total_bytes','epoch','processed','expires_at')) or
            response['capacity_settled'] is not False or self.transfer.wall()>=response['expires_at'] or
            not 0<=response['processed']<=max(8,unit['object_count']) or
            not isinstance(response['state'],str) or response['state'] not in {'pending','copying','verified','publishing','published'} or
            not isinstance(response['phase'],str) or response['phase'] not in {'metadata','roster','assembling','scan_private','move','register','scan_public','published'}):
            raise TransferError('migration_publish_target_reply_invalid')
        if response['state']=='published':
            if response['phase']!='published':raise TransferError('migration_publish_target_reply_invalid')
            identifier(response['receipt_sha256'],long=True)
        elif response['receipt_sha256']!='':raise TransferError('migration_publish_target_reply_invalid')
        return response

    async def target_step(self,lease,unit_id):
        identifier(unit_id,long=True)
        await self.transfer.journal.renew(lease,ttl=60)
        async with self.store.database.begin() as conn:
            task=await self.transfer.journal._fenced(conn,lease)
            if task['state']!='committing':raise TransferError('migration_publish_requires_committing')
            unit=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id).with_for_update())).mappings().first()
            if not unit or unit['state'] not in {'pending','copying','verified','publishing'}:
                raise TransferError('migration_publish_unit_unavailable')
            selected=await pipeline.current(conn,task)
            if selected is None or selected['unit_id']!=unit_id:raise TransferError('migration_publish_unit_unavailable')
            if unit['state']!='publishing':await pipeline.verified(conn,task,unit)
            unit=dict(unit);after=unit['publication'].get('metadata_after','')
            rows=(await conn.execute(select(s.plan_bindings).where(s.plan_bindings.c.plan_id==task['task_id'],
                s.plan_bindings.c.unit_id==unit_id,s.plan_bindings.c.logical_id>after).order_by(s.plan_bindings.c.logical_id).limit(8))).mappings().all()
            entries=[dict(logical_id=row['logical_id'],encryption=json.loads(row['object_json'])['encryption']) for row in rows]
        relations=await self.transfer.relations(task)
        if entries:result=await self._call(task,relations,lease,unit,'metadata',entries=entries)
        else:result=await self._call(task,relations,lease,unit,'unit',unit_id=unit_id)
        async with self.store.database.begin() as conn:
            await self.transfer.journal._fenced(conn,lease)
            current=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id).with_for_update())).mappings().one()
            if current['publication']!=unit['publication']:raise TransferError('migration_publish_state_changed')
            publication={**current['publication'],'target_reply':result}
            if rows:publication['metadata_after']=rows[-1]['logical_id']
            await conn.execute(update(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id)
                .values(publication=publication,state='publishing'))
            await self.store.log(conn,'migration_target_publication_step',lease.runner_id,task_id=task['task_id'],unit_id=unit_id,phase=result['phase'])
        return dict(task_id=task['task_id'],unit_id=unit_id,target_published=result['state']=='published',
            phase=result['phase'],cutover=False,capacity_settled=False)

    async def source_step(self,lease,unit_id):
        identifier(unit_id,long=True)
        await self.transfer.journal.renew(lease,ttl=60)
        async with self.store.database.begin() as conn:
            task=await self.transfer.journal._fenced(conn,lease)
            unit=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id))).mappings().first()
            if (task['state']!='committing' or not unit or unit['state']!='publishing' or
                    unit['publication'].get('target_reply',{}).get('state')!='published'):
                raise TransferError('migration_verify_requires_target_publication')
            unit=dict(unit)
        relations=await self.transfer.relations(task);request_id=uuid.uuid4().hex
        grant=self.transfer._grant(task,relations,lease.epoch,lease.runner_id,'verify')
        result=await signed_request(self.store,relations[task['source_id']],self.transport,
            '/internal/v1/migration/transfer/verify-source',dict(request_id=request_id,grant=grant,unit_id=unit_id),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_verify')
        expected=dict(format='frontiercloud-python-source-verification',version=1,request_id=request_id,
            node_id=task['source_id'],task_id=task['task_id'],unit_id=unit_id,unit_version=unit['source_version'],
            object_count=unit['object_count'],total_bytes=unit['total_bytes'],epoch=lease.epoch,
            expires_at=grant['payload']['expires_at'],ownership_changed=False)
        if (not isinstance(result,dict) or result.keys()!={*expected,'phase','processed_objects','receipt_sha256'} or
                any(result.get(name)!=value for name,value in expected.items()) or
                any(type(result[name]) is not int for name in ('version','object_count','total_bytes','epoch','expires_at','processed_objects')) or
                result['ownership_changed'] is not False or self.transfer.wall()>=result['expires_at'] or
                not isinstance(result['phase'],str) or result['phase'] not in {'objects','membership','verified'} or
                not 0<=result['processed_objects']<=unit['object_count']):
            raise TransferError('migration_verify_source_reply_invalid')
        if result['phase']=='verified':
            if result['processed_objects']!=unit['object_count']:raise TransferError('migration_verify_source_reply_invalid')
            identifier(result['receipt_sha256'],long=True)
        elif result['receipt_sha256']!='':raise TransferError('migration_verify_source_reply_invalid')
        async with self.store.database.begin() as conn:
            await self.transfer.journal._fenced(conn,lease)
            current=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id).with_for_update())).mappings().one()
            if current['state']!='publishing' or current['publication']!=unit['publication']:
                raise TransferError('migration_publish_state_changed')
            await conn.execute(update(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id)
                .values(publication={**current['publication'],'source_reply':result}))
            await self.store.log(conn,'migration_source_verification_step',lease.runner_id,task_id=task['task_id'],unit_id=unit_id,phase=result['phase'])
        return dict(task_id=task['task_id'],unit_id=unit_id,source_verified=result['phase']=='verified',
            phase=result['phase'],cutover=False,capacity_settled=False)
