"""Trusted TLS Target release survives prepared and committed process restarts."""
import asyncio
import hashlib
import time
import uuid

import httpx
from sqlalchemy import func,select

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_target_receipts import released,source_record
from app.services.resource_control_client import signed_request
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError


async def finish(f,transport,catalog,lease,originals,restart_target,heartbeat,check):
    if restart_target is None:raise RuntimeError('Target release requires an independent Target process')
    async with f.master.database.begin() as conn:task=await catalog.t.journal._fenced(conn,lease)
    relations=await catalog.t.relations(task)
    def grant():return catalog.t._grant(task,relations,lease.epoch,lease.runner_id,'finalize')
    reply=await signed_request(f.master.store,relations[task['source_id']],transport,
        '/internal/v1/migration/transfer/cancel-source-release',dict(request_id=uuid.uuid4().hex,grant=grant(),agreement=None,source_census=None),
        limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel_release')
    witness=dict(version=1,agreement=reply['agreement'],source_census=reply['source_census'],receipt=reply['receipt'])
    async with f.target.database.connect() as conn:
        before=dict((await conn.execute(select(fs.storage_members))).mappings().one())
        committed=list(await conn.scalars(select(s.units.c.unit_id).where(s.units.c.state=='committed')))
        objects=(await conn.execute(select(s.objects).where(s.objects.c.unit_id.in_(committed)))).mappings().all()
    public=[(f.target.roots['media']/obj['physical_path'],obj['source_sha256']) for obj in objects]
    public_before=[(path,hashlib.sha256(path.read_bytes()).hexdigest()) for path,_ in public]
    check('actual_target_release_starts_with_verified_committed_public_bytes',all(actual==obj[1] for (_,actual),obj in zip(public_before,public)))
    path='/internal/v1/migration/transfer/cancel-target-release'
    async def step():
        return await signed_request(f.master.store,relations[task['target_id']],transport,path,
            dict(request_id=uuid.uuid4().hex,grant=grant(),source_record=witness),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel_target_release')
    quiet_waits=[]
    async def ready_step():
        for _ in range(8):
            await heartbeat()
            try:return await step()
            except TransferError as exc:
                if not str(exc).endswith(('waiting_low_traffic','low_traffic')):raise
                started=time.monotonic();await asyncio.sleep(30.1);quiet_waits.append(time.monotonic()-started)
        raise AssertionError('Target release did not observe a fresh quiet window')
    for _ in range(100):
        result=await ready_step()
        if result['phase']=='prepared':break
    else:raise AssertionError('Target release did not prepare')
    await restart_target();await heartbeat()
    check('actual_target_release_preparation_survives_target_process_restart',True)
    old_request=transport.request;lost=None
    async def discard(origin,path,**kwargs):
        nonlocal lost
        result=await old_request(origin,path,**kwargs)
        if path.endswith('/cancel-target-release') and result['payload']['phase']=='released' and lost is None:
            lost=result['payload'];raise httpx.ReadTimeout('Fixture discarded committed native Target release')
        return result
    transport.request=discard
    try:
        try:await ready_step()
        except httpx.ReadTimeout:pass
        else:raise AssertionError('Target release reply was not discarded')
    finally:transport.request=old_request
    check('actual_target_release_lost_reply_follows_committed_native_receipt',lost is not None)
    await restart_target();await heartbeat();result=await ready_step();value=grant()['payload']
    source=source_record(witness,value,(task['plan']['unit_count'],task['object_count'],task['total_bytes']),
        p.public_key(f.master.store.unseal(f.master.store.node['private_key'])),time.time())
    receipt=released(result['receipt'],value,source,witness,result['preparation']['payload']['roots_sha256'],result['preparation'],time.time())
    check('actual_target_release_recovers_original_receipt_after_real_restart',result['phase']=='released' and
        result['receipt']==lost['receipt'] and result['preparation']==lost['preparation'] and result['source_record']==witness)
    check('actual_target_release_observes_new_quiet_window_after_restart',len(quiet_waits)>=1 and all(seconds>=30 for seconds in quiet_waits))
    async with f.target.database.connect() as conn:
        after=dict((await conn.execute(select(fs.storage_members))).mappings().one())
        check('actual_target_release_preserves_exact_native_used_and_reserved_bytes',
            (after['used_bytes'],after['reserved_bytes'])==(before['used_bytes'],before['reserved_bytes']) and
            receipt['native_used_before']==receipt['native_used_after'])
        check('actual_target_release_removes_only_owned_native_control_material',all([
            await conn.scalar(select(func.count()).select_from(table))==0 for table in (s.capacity,s.scopes,s.drains)]))
        check('actual_target_release_is_durable_and_replay_has_one_audit',await conn.scalar(select(s.peer_reservations.c.state))=='released' and
            await conn.scalar(select(func.count()).select_from(fs.audit).where(fs.audit.c.action=='migration_native_cancel_target_released'))==1)
    check('actual_target_release_preserves_all_committed_and_retained_bytes',
        all(hashlib.sha256(path.read_bytes()).hexdigest()==sha for path,sha in public_before) and
        all(path.read_bytes()==data for _,path,data in originals))
    async with f.master.database.connect() as conn:
        check('actual_target_release_does_not_finish_master_or_remove_master_drain',await conn.scalar(select(s.tasks.c.state))=='cancelling' and
            await conn.scalar(select(func.count()).select_from(s.drains))==1)
    return dict(native_target_release=True,target_release_actual_https=True,target_release_process_restarts=2,
        target_release_quiet_wait_seconds=quiet_waits,target_release_lost_reply_recovered=True,
        master_terminal_cancellation=False,full_cancellation_acceptance=False)
