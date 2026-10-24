"""Verified TLS release, lost reply and a real Source process restart."""
import ssl
import time
import uuid

import httpx
from sqlalchemy import func,select

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_source_receipts import verify
from app.services.resource_control_client import signed_request
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError


async def finish(f,transport,catalog,lease,originals,restart_source,heartbeat,check):
    authority=await catalog.authorize(lease)
    async with f.master.database.begin() as conn:task=await catalog.t.journal._fenced(conn,lease)
    relations=await catalog.t.relations(task);relation=relations[task['source_id']]
    old_request=transport.request;lost=None
    async def discard(origin,path,**kwargs):
        nonlocal lost
        result=await old_request(origin,path,**kwargs)
        if path.endswith('/cancel-source-release') and result['payload']['phase']=='released' and lost is None:
            lost=result['payload'];raise httpx.ReadTimeout('Fixture discarded committed native Source release')
        return result
    path='/internal/v1/migration/transfer/cancel-source-release'
    transport.request=discard
    try:
        try:
            await signed_request(f.master.store,relation,transport,path,dict(request_id=uuid.uuid4().hex,**authority),
                limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel_release')
        except httpx.ReadTimeout:pass
        else:raise AssertionError('Source release reply was not discarded')
    finally:transport.request=old_request
    check('actual_source_release_lost_reply_follows_a_committed_receipt',lost is not None)
    await restart_source();await heartbeat()
    grant=catalog.t._grant(task,relations,lease.epoch,lease.runner_id,'finalize')
    request_id=uuid.uuid4().hex
    reply=await signed_request(f.master.store,relation,transport,path,dict(request_id=request_id,grant=grant,agreement=None,source_census=None),
        limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel_release')
    check('actual_source_release_restarts_without_repeating_census_or_release',reply['phase']=='released' and reply['request_id']==request_id
        and reply['receipt']==lost['receipt'] and reply['agreement']==lost['agreement'] and reply['source_census']==lost['source_census'])
    receipt=verify(reply['receipt'],grant['payload'],(task['plan']['unit_count'],task['object_count'],task['total_bytes']),time.time(),
        master_key=p.public_key(f.master.store.unseal(f.master.store.node['private_key'])),
        agreement=reply['agreement'],source_census=reply['source_census'])
    retained=sum(len(data) for _,_,data in originals)
    check('actual_source_release_receipt_binds_retained_usage_and_original_counts',receipt['native_used_after']==retained
        and receipt['retained_objects']==len(originals) and receipt['object_count']==task['object_count'])
    async with f.source.database.connect() as conn:
        check('actual_source_release_reconciles_only_native_source_usage',await conn.scalar(select(fs.storage_members.c.used_bytes))==retained)
        check('actual_source_release_native_header_is_durably_released',await conn.scalar(select(s.peer_reservations.c.state))=='released')
        check('actual_source_release_clears_only_owned_native_control_material',all([
            await conn.scalar(select(func.count()).select_from(table))==0 for table in (s.capacity,s.scopes,s.drains)]))
        check('actual_source_release_replay_has_exactly_one_release_audit',await conn.scalar(select(func.count()).select_from(fs.audit).where(
            fs.audit.c.action=='migration_native_cancel_source_released'))==1)
    check('actual_source_release_preserves_every_retained_plain_and_encrypted_byte',all(path.read_bytes()==data for _,path,data in originals))
    async with f.master.database.connect() as conn:
        row=(await conn.execute(select(fs.global_media).where(fs.global_media.c.media_id==originals[0][0]))).mappings().one()
        check('actual_source_release_does_not_finish_master_or_release_master_drain',await conn.scalar(select(s.tasks.c.state))=='cancelling'
            and await conn.scalar(select(func.count()).select_from(s.drains))==1)
    token=p.media_token(f.source.credential,f.source.relationship,f.master.store.node['node_id'],f.source.store.node['node_id'],
        row['object_id'],row['media_id'],int(time.time()))
    async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(f.root/'ca.pem')),trust_env=False,timeout=5) as reader:
        response=await reader.get(f.source.store.node['endpoint']+'/internal/v1/media/'+row['object_id'],
            headers={'X-Media-Capability':token,'Range':'bytes=3-9','Origin':f.master.store.node['endpoint']})
    check('actual_source_release_keeps_normal_retained_range_readable',response.status_code==206 and response.content==originals[0][2][3:10])
    return dict(native_source_release=True,source_release_actual_https=True,source_release_lost_reply_recovered=True,
        source_release_process_restarts=1,native_usage_reconciled=True,source_drain_released=True,master_terminal_cancellation=False,
        full_cancellation_acceptance=False)
