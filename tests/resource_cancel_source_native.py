"""Verified HTTPS Source retention with real process and quiet-window recovery."""
import asyncio
import json
import os
import ssl
import time
import uuid

import httpx
from sqlalchemy import select

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p
from app.services.resource_cancel_source import CancellationSource
from app.services.resource_cancel_source_peers import selected,verify_receipt
from app.services.resource_control_client import signed_request
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError


async def finish(f,transport,traffic,lease,units,originals,target_receipts,restart_source,check,inputs,domain,*,restart_target=None):
    authority=CancellationSource(f.master.store,transport,traffic=traffic).authority
    source=authority.transfer;receipts={};restarts=0;before=await domain()

    async def heartbeat():
        for peer in (f.source,f.target):
            summary=await transport.request(peer.store.node['endpoint'],'/internal/v1/heartbeat',method='POST',value={},
                credential=peer.credential,relation=peer.relationship)
            await f.master.store.heartbeat(peer.relationship,True,summary=summary)

    async def call(action,unit_id=None,*,replay=False):
        await heartbeat();await source.journal.renew(lease,ttl=60)
        async with f.master.database.begin() as conn:task=await source.journal._fenced(conn,lease)
        relations=await source.relations(task);grant=source._grant(task,relations,lease.epoch,lease.runner_id,'cancel')
        value=dict(request_id=uuid.uuid4().hex,grant=grant)
        if unit_id is not None:
            authorization=None if replay else (await authority.issue(lease,unit_id))[0]
            value.update(unit_id=unit_id,authorization=authorization,target_receipt=None if replay else target_receipts[unit_id])
        result=await signed_request(f.master.store,relations[task['source_id']],transport,'/internal/v1/migration/transfer/'+action,
            value,limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel')
        return result,grant

    await restart_source();restarts+=1
    result,_=await call('cancel-source-stop')
    check('actual_source_process_stop_is_light_during_startup_quiet',result['phase']=='stopped' and result['source_files_removed'] is False)
    waiting,_=await call('cancel-source-retain',units[0])
    check('actual_source_retention_waits_for_startup_quiet',waiting['phase']=='waiting_low_traffic')
    await asyncio.sleep(30.1)
    first,_=await call('cancel-source-retain',units[0])
    check('actual_source_metadata_page_precedes_durable_retention',first['phase']=='objects' and first['receipt'] is None)
    await restart_source();restarts+=1
    waiting,_=await call('cancel-source-retain',units[0])
    check('source_restart_keeps_stop_fence_and_repeats_real_quiet_window',waiting['phase']=='waiting_low_traffic')
    await asyncio.sleep(30.1)
    for index,unit_id in enumerate(units):
        for _ in range(24):
            result,grant=await call('cancel-source-retain',unit_id)
            if result['phase']=='retained':
                async with f.source.database.connect() as conn:
                    unit=dict((await conn.execute(select(s.units).where(s.units.c.task_id==lease.task_id,s.units.c.unit_id==unit_id))).mappings().one())
                verify_receipt(result['receipt'],p.public_key(f.master.store.unseal(f.master.store.node['private_key'])),
                    grant['payload'],selected(unit,grant['payload']),time.time(),target_receipt=target_receipts[unit_id])
                receipts[unit_id]=result['receipt'];break
        else:raise AssertionError('Actual HTTPS Source retention did not converge')
        check('actual_tls_signed_source_retention_unit_'+str(index),result['source_files_removed'] is False and result['capacity_settled'] is False)
    check('source_retention_keeps_original_plain_and_ciphertext_bytes',all(path.read_bytes()==data for _,path,data in originals))
    check('source_retention_preserves_all_business_identities_quota_and_fences',await domain()==before)
    previous=next(row for row in reversed(inputs) if row['path'].endswith('/cancel-source-retain'))
    later=originals[0][1].with_name('later-source-external.bin');later.write_bytes(b'preserve later Source material on replay')
    await restart_source();restarts+=1
    context=ssl.create_default_context(cafile=str(f.root/'ca.pem'))
    async with httpx.AsyncClient(verify=context,trust_env=False,timeout=5) as client:
        replay=await client.post(previous['url'],content=p.canonical(previous['value']),headers=previous['headers'])
        check('source_process_restart_retains_hmac_nonce_replay_protection',replay.status_code==401)
    result,_=await call('cancel-source-retain',units[0],replay=True)
    check('source_signed_receipt_replays_before_new_quiet_window',result['phase']=='retained' and result['receipt']==receipts[units[0]])
    check('source_terminal_replay_preserves_later_external_material',later.read_bytes()==b'preserve later Source material on replay')
    check('source_terminal_replay_keeps_all_quota_and_fences',await domain()==before)
    settlement={}
    if os.environ.get('FRONTIERCLOUD_UNIT_SETTLE_NATIVE')=='1':
        if restart_target is None:raise RuntimeError('Unit settlement requires an independent Target restart')
        # These exact test-owned injections already passed preservation checks.
        # Retain them outside managed roots before requesting fresh settlement.
        source_kept=f.root/'source-replay-injection-kept';later.rename(source_kept)
        target_later=f.target.roots['media']/originals[0][1].relative_to(f.source.roots['media'])
        assert list(target_later.parent.iterdir())==[target_later]
        target_kept=f.root/'target-replay-injection-kept';target_later.parent.rename(target_kept)
        check('fixture_replay_injections_retained_outside_selected_parents',source_kept.read_bytes()==b'preserve later Source material on replay' and
            (target_kept/target_later.name).read_bytes()==b'later external material must survive terminal replay')
        from tests.resource_cancel_unit_native import finish as settle_units
        settlement=await settle_units(f,transport,traffic,lease,units,originals,receipts,target_receipts,restart_source,restart_target,check,domain)
    return dict(native_source_retention=True,source_process_restarts=restarts,source_unit_receipts=len(receipts),
        **dict(dict(full_cancellation_acceptance=False),**settlement))
