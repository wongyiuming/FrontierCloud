"""Retain the actual Source signature; Master cannot substitute its own proof."""
import uuid

from sqlalchemy import update

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_authority import CancellationAuthority,verify as verify_authorization
from app.services.resource_control_client import signed_request
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import CONTROL_BYTES,WINDOW_SECONDS,TransferError,identifier


def reply(envelope,grant,unit,now,*,request_id=None):
    try:value=p.verify(grant['source_key'],envelope)
    except (p.ProtocolError,TypeError,KeyError):raise TransferError('migration_cancel_source_proof_invalid') from None
    expected=dict(format='frontiercloud-python-source-verification',version=1,node_id=grant['source_id'],
        task_id=grant['task_id'],unit_id=unit['unit_id'],unit_version=unit['unit_version'],
        object_count=unit['object_count'],total_bytes=unit['total_bytes'],epoch=grant['epoch'],ownership_changed=False)
    if (not isinstance(value,dict) or value.keys()!={*expected,'request_id','phase','processed_objects','expires_at','receipt_sha256'} or
            any(value.get(name)!=item for name,item in expected.items()) or
            any(type(value[name]) is not int for name in ('version','epoch','object_count','total_bytes','processed_objects','expires_at')) or
            type(value['ownership_changed']) is not bool or not isinstance(value['phase'],str) or
            value['phase'] not in {'objects','membership','verified'} or not 0<=value['processed_objects']<=unit['object_count'] or
            request_id is not None and (value['request_id']!=request_id or value['expires_at']!=grant['expires_at']) or
            value['expires_at']-WINDOW_SECONDS>now+5):
        raise TransferError('migration_cancel_source_proof_invalid')
    identifier(value['request_id'])
    if now>=value['expires_at']:raise TransferError('migration_cancel_source_proof_expired')
    if value['phase']=='verified':
        identifier(value['receipt_sha256'],long=True)
        if value['processed_objects']!=unit['object_count']:raise TransferError('migration_cancel_source_proof_incomplete')
    elif value['receipt_sha256']!='':raise TransferError('migration_cancel_source_proof_invalid')
    return value


def verify(envelope,master_key,authorization,grant,unit,now):
    permission=verify_authorization(authorization,master_key,grant,unit,now)
    value=reply(envelope,grant,unit,now)
    if (value['phase']!='verified' or value['expires_at']>permission['expires_at'] or
            value['expires_at']-WINDOW_SECONDS<permission['created_at']-5):
        raise TransferError('migration_cancel_source_proof_not_current')
    return value


class CancellationSource:
    def __init__(self,store,transport,**options):
        self.authority=CancellationAuthority(store,transport,**options)
        self.transfer=self.authority.transfer;self.store,self.transport=store,transport

    async def advance(self,lease,unit_id):
        authorization,grant,selected=await self.authority.issue(lease,unit_id)
        async with self.store.database.begin() as conn:
            task=await self.transfer.journal._fenced(conn,lease)
            relations=await self.transfer.relations(task,conn=conn)
        # The existing verification endpoint can prove retained originals. Only
        # the explicit Master permission supplies cancellation/owner authority.
        grant={**grant,'purpose':'verify'}
        signed=p.sign(self.store.unseal(self.store.node['private_key']),grant);request_id=uuid.uuid4().hex
        envelope=await signed_request(self.store,relations[task['source_id']],self.transport,
            '/internal/v1/migration/transfer/verify-source',dict(request_id=request_id,grant=signed,unit_id=unit_id),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel',return_envelope=True)
        value=reply(envelope,grant,selected,self.transfer.wall(),request_id=request_id)
        async with media_mutation_lock,self.store.database.begin() as conn:
            current=await self.transfer.journal._fenced(conn,lease)
            if current['state']!='cancelling':raise TransferError('migration_cancel_request_required')
            unit=await self.authority.cutover._unit(conn,current,unit_id)
            if unit['committed_at'] or unit['cleanup_after'] or unit['publication'].get('commit'):
                raise TransferError('migration_cancel_master_commit_is_final')
            await self.authority.cutover._scopes(conn,current,unit)
            await self.authority.cutover._source_catalog(conn,current,unit,require_copy=False)
            proof=None
            if value['phase']=='verified':
                verify(envelope,p.public_key(self.store.unseal(self.store.node['private_key'])),
                    authorization,grant,selected,self.transfer.wall())
                proof=dict(authorization=authorization,source_reply=envelope)
                await conn.execute(update(s.units).where(s.units.c.task_id==lease.task_id,s.units.c.unit_id==unit_id)
                    .values(publication={**unit['publication'],'cancel_source':proof}))
            await self.store.log(conn,'migration_cancel_source_observed',lease.runner_id,
                task_id=lease.task_id,unit_id=unit_id,phase=value['phase'],proof_sha256=digest(proof) if proof else '')
        return dict(phase=value['phase'],processed_objects=value['processed_objects'],proof=proof,source_files_removed=False)
