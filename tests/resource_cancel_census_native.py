"""Actual TLS current-census inputs and Source process recovery, without release."""
import asyncio
import os
import ssl
import time

import httpx
from sqlalchemy import func,select

from app.services import resource_fences,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_census_client import CensusClient
from app.services.resource_cancel_catalog import CurrentCatalog,MARKER
from app.services.resource_migration_journal import Lease
from app.services.resource_transfer_protocol import TransferError


async def finish(f,transport,traffic,originals,restart_source,check,inputs,*,restart_target=None):
    async with f.master.database.connect() as conn:
        task=(await conn.execute(select(s.tasks))).mappings().one();runner=(await conn.execute(select(s.runners))).mappings().one()
    lease=Lease(task['task_id'],runner['runner_id'],runner['epoch'],task['revision'])
    client=CensusClient(f.master.store,transport,traffic=traffic);phases=[];restarted=False;lost=False;lost_proof=None
    async with f.source.database.connect() as conn:used=await conn.scalar(select(fs.storage_members.c.used_bytes))
    async def heartbeat():
        for peer in (f.source,f.target):
            summary=await transport.request(peer.store.node['endpoint'],'/internal/v1/heartbeat',method='POST',value={},
                credential=peer.credential,relation=peer.relationship)
            await f.master.store.heartbeat(peer.relationship,True,summary=summary)
        await client.t.journal.renew(lease,ttl=60)
    old_request=transport.request
    async def discard(origin,path,**kwargs):
        nonlocal lost,lost_proof
        value=await old_request(origin,path,**kwargs)
        if path.endswith('/cancel-source-census') and value['payload']['phase']=='verified' and not lost:
            lost=True;lost_proof=value['payload']['proof'];raise httpx.ReadTimeout('Fixture discarded a received signed census proof')
        return value
    async def ready():
        nonlocal restarted
        for _ in range(100):
            await heartbeat()
            try:value=await client.query(lease)
            except httpx.ReadTimeout:continue
            except TransferError as exc:
                if not str(exc).endswith(('waiting_low_traffic','low_traffic')):raise
                await asyncio.sleep(30.1);continue
            phases.append(value['phase'])
            if value['phase']=='physical' and not restarted:
                await restart_source();restarted=True;continue
            if value['phase']=='verified':return value
        raise AssertionError('Actual TLS current census did not converge')
    transport.request=discard
    try:first=await ready()
    finally:transport.request=old_request
    check('actual_census_source_restarts_after_roster_and_repeats_quiet',restarted)
    check('actual_census_lost_verified_reply_replays_original_signed_proof',lost and first['proof']==lost_proof)
    check('actual_census_typed_counts_match_all_retained_plain_and_ciphertext',
        first['retained_objects']==len(originals) and first['retained_bytes']==sum(len(data) for _,_,data in originals))
    check('actual_census_does_not_claim_master_catalog_or_release',first['master_catalog_verified'] is False and first['task_completed'] is False and
        first['native_usage_reconciled'] is False and first['source_drain_released'] is False)
    async with f.master.database.connect() as conn:
        row=(await conn.execute(select(fs.global_media).where(fs.global_media.c.media_id==originals[0][0]))).mappings().one()
    token=p.media_token(f.source.credential,f.source.relationship,f.master.store.node['node_id'],f.source.store.node['node_id'],
        row['object_id'],row['media_id'],int(time.time()))
    async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(f.root/'ca.pem')),trust_env=False,timeout=5) as reader:
        response=await reader.get(f.source.store.node['endpoint']+'/internal/v1/media/'+row['object_id'],
            headers={'X-Media-Capability':token,'Range':'bytes=3-9','Origin':f.master.store.node['endpoint']})
    check('actual_census_fence_keeps_ordinary_retained_source_range_readable',response.status_code==206 and response.content==originals[0][2][3:10])
    unknown=f.source.roots['media']/'fixture-owner-extra.bin';unknown.write_bytes(b'Preserve unknown owner bytes')
    deadline=first['proof']['payload']['expires_at']+1
    while time.time()<deadline:
        await heartbeat();await asyncio.sleep(min(10,max(0,deadline-time.time())))
    try:
        await ready()
    except TransferError as exc:check('actual_census_expired_proof_cannot_hide_new_unknown_file',str(exc)=='migration_cancel_census_unknown_file')
    else:raise AssertionError('Current census accepted unknown owner data')
    check('actual_census_unknown_file_is_preserved',unknown.read_bytes()==b'Preserve unknown owner bytes')
    retained=f.root/'fixture-owner-retained-census-file.bin';unknown.rename(retained)
    final=await ready()
    check('actual_census_retries_only_after_explicit_fixture_owner_resolution',retained.read_bytes()==b'Preserve unknown owner bytes' and
        final['proof']['payload']['census_id']!=first['proof']['payload']['census_id'])
    catalog=CurrentCatalog(f.master.store,transport,traffic=traffic)
    async with f.master.database.connect() as conn:
        original_usage=[tuple(row) for row in await conn.execute(select(fs.storage_members.c.member_id,
            fs.storage_members.c.used_bytes,fs.storage_members.c.reserved_bytes).order_by(fs.storage_members.c.member_id))]
    await heartbeat();await catalog.advance(lease)
    async with f.master.database.begin() as conn:
        blocked=False
        try:await resource_fences.paths(conn,'media',[(row['media_path'],False)])
        except resource_fences.FenceConflict:blocked=True
    check('actual_master_current_census_installs_write_fence_before_source_query',blocked)
    catalog=CurrentCatalog(f.master.store,transport,traffic=traffic)
    master_lost=False;master_lost_proof=None
    async def discard_master(origin,path,**kwargs):
        nonlocal master_lost,master_lost_proof
        value=await old_request(origin,path,**kwargs)
        if path.endswith('/cancel-source-census') and value['payload']['phase']=='verified' and not master_lost:
            master_lost=True;master_lost_proof=value['payload']['proof']
            raise httpx.ReadTimeout('Fixture discarded Source proof before Master commit')
        return value
    transport.request=discard_master
    try:
        for _ in range(100):
            await heartbeat()
            try:agreement=await catalog.advance(lease)
            except httpx.ReadTimeout:continue
            except TransferError as exc:
                if not str(exc).endswith(('waiting_low_traffic','low_traffic')):raise
                await asyncio.sleep(30.1);continue
            if agreement['master_catalog_verified']:break
        else:raise AssertionError('Actual TLS Master current catalog did not converge')
    finally:transport.request=old_request
    async with f.master.database.connect() as conn:
        progress=(await conn.scalar(select(s.tasks.c.plan)))[MARKER]
        current_usage=[tuple(item) for item in await conn.execute(select(fs.storage_members.c.member_id,
            fs.storage_members.c.used_bytes,fs.storage_members.c.reserved_bytes).order_by(fs.storage_members.c.member_id))]
    check('actual_master_current_census_restarts_object_and_recovers_lost_source_proof',master_lost and progress['source_proof']==master_lost_proof)
    check('actual_master_catalog_matches_signed_native_current_roster',progress['catalog_sha256']==progress['source_proof']['payload']['catalog_sha256'] and
        agreement['retained_objects']==len(originals) and agreement['retained_bytes']==sum(len(data) for _,_,data in originals))
    check('actual_master_current_census_preserves_all_member_usage_and_reservations',current_usage==original_usage)
    check('actual_master_agreement_has_no_task_or_native_release_authority',agreement['native_usage_reconciled'] is False and
        agreement['source_drain_released'] is False and agreement['task_completed'] is False)
    check('actual_census_preserves_original_plain_and_ciphertext',all(path.read_bytes()==data for _,path,data in originals))
    async with f.source.database.connect() as conn:
        check('actual_census_preserves_source_usage_and_task_drain',await conn.scalar(select(fs.storage_members.c.used_bytes))==used and
            await conn.scalar(select(s.peer_reservations.c.state))=='active' and await conn.scalar(select(func.count()).select_from(s.drains))==1)
    async with f.master.database.connect() as conn:
        check('actual_census_keeps_master_cancelling',await conn.scalar(select(s.tasks.c.state))=='cancelling')
    result=dict(native_current_source_census=True,census_source_process_restarts=1,census_lost_reply_recovered=True,
        census_native_payload_reads_forbidden=True,census_stages=sorted(set(phases)),master_catalog_verified=True,
        master_current_catalog_native_tls=True,master_current_catalog_lost_reply_recovered=True,
        native_usage_reconciled=False,source_drain_released=False,master_terminal_cancellation=False,full_cancellation_acceptance=False,
        actual_http_inputs=inputs)
    if os.environ.get('FRONTIERCLOUD_CANCEL_SOURCE_RELEASE_NATIVE')=='1':
        from tests.resource_cancel_source_release_native import finish as release_source
        result.update(await release_source(f,transport,catalog,lease,originals,restart_source,heartbeat,check))
    if os.environ.get('FRONTIERCLOUD_CANCEL_TARGET_RELEASE_NATIVE')=='1':
        from tests.resource_cancel_target_release_native import finish as release_target
        result.update(await release_target(f,transport,catalog,lease,originals,restart_target,heartbeat,check))
    if os.environ.get('FRONTIERCLOUD_CANCEL_MASTER_RELEASE_NATIVE')=='1':
        from tests.resource_cancel_finalize_master_native import finish as release_master
        result.update(await release_master(f,transport,traffic,lease,originals,heartbeat,check))
    return result
