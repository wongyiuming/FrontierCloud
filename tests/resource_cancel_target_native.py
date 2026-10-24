"""Actual verified-TLS Target rollback, readers and independent process restart.

The fixture deliberately installs a cancelling Master phase. It proves native
Target recovery, not the still-gated public mixed-task cancellation workflow.
"""
import copy
import json
import ssl
import time
import uuid

import httpx
from sqlalchemy import func,select,update

from app.services import media_crypto_schema as crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_source import CancellationSource
from app.services.resource_cancel_target import selected,verify_receipt
from app.services.resource_control_client import signed_request
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError


async def finish(f,transport,traffic,lease,units,originals,restart_target,check,inputs,*,restart_source=None):
    import asyncio
    # Public journal control still refuses post-publication cancellation. This
    # controlled phase is explicitly distinct from product-level acceptance.
    async with media_mutation_lock,f.master.database.begin() as conn:
        task=dict((await conn.execute(select(s.tasks).where(s.tasks.c.task_id==lease.task_id))).mappings().one())
        assert task['state']=='committing' and task['committed_bytes']==0
        await conn.execute(update(s.tasks).where(s.tasks.c.task_id==lease.task_id).values(state='cancelling',revision=task['revision']+1))
        await conn.execute(update(s.runners).where(s.runners.c.task_id==lease.task_id).values(expires_at=0))
    source=CancellationSource(f.master.store,transport,traffic=traffic)
    lease=await source.transfer.journal.claim(lease.task_id,uuid.uuid4().hex,ttl=60)
    async def heartbeat():
        for peer in (f.source,f.target):
            summary=await transport.request(peer.store.node['endpoint'],'/internal/v1/heartbeat',method='POST',value={},
                credential=peer.credential,relation=peer.relationship)
            await f.master.store.heartbeat(peer.relationship,True,summary=summary)
    await heartbeat()
    async with f.master.database.begin() as conn:task=await source.transfer.journal._fenced(conn,lease)
    relations=await source.transfer.relations(task)
    target=relations[task['target_id']]
    master_key=p.public_key(f.master.store.unseal(f.master.store.node['private_key']))
    receipts={};restart_count=0;proofs={}

    async def domain():
        result={}
        for peer in (f.master,f.source,f.target):
            async with peer.database.connect() as conn:
                tables=(fs.identity,s.capacity,s.scopes,s.drains)
                if peer.name!='target':tables=(*tables,fs.global_media,crypto.encryption)
                result[peer.name]={table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in tables}
                columns=[fs.storage_members.c[name] for name in ('member_id','relationship_id','member_kind','transport','storage_enabled',
                    'allocated_bytes','used_bytes','reserved_bytes','writable')]
                result[peer.name][fs.storage_members.name]=[dict(row) for row in (await conn.execute(select(*columns))).mappings()]
        return copy.deepcopy(result)

    before=await domain()
    async with f.target.database.connect() as conn:
        published_encrypted=await conn.scalar(select(func.count()).select_from(crypto.encryption))

    async def proof(unit_id):
        await heartbeat()
        for _ in range(24):
            result=await source.advance(lease,unit_id)
            if result['phase']=='verified':proofs[unit_id]=result['proof'];return result['proof']
        raise AssertionError('Actual HTTPS Source proof did not converge')

    async def call(action,unit_id=None,*,permission=True):
        await source.transfer.journal.renew(lease,ttl=60)
        grant=source.transfer._grant(task,relations,lease.epoch,lease.runner_id,'cancel')
        value=dict(request_id=uuid.uuid4().hex,grant=grant)
        if unit_id is not None:value.update(unit_id=unit_id,proof=proofs[unit_id] if permission else None)
        result=await signed_request(f.master.store,target,transport,'/internal/v1/migration/transfer/'+action,value,
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel')
        return result,grant

    async def read(logical,obj):
        # A short, valid capability tests actual expiry without changing any
        # product clock or deleting a durable reader lease from the fixture.
        token=p.media_token(f.target.credential,f.target.relationship,f.master.store.node['node_id'],
            f.target.store.node['node_id'],obj['source_object'],logical,int(time.time())-p.TOKEN_SECONDS+20)
        context=ssl.create_default_context(cafile=str(f.root/'ca.pem'))
        async with httpx.AsyncClient(verify=context,trust_env=False,timeout=5) as client:
            return await client.get(f.target.store.node['endpoint']+'/internal/v1/media/'+obj['source_object'],
                headers={'X-Media-Capability':token,'Range':'bytes=3-9','Origin':f.master.store.node['endpoint']})

    first=originals[0]
    async with f.target.database.connect() as conn:
        obj=json.loads(await conn.scalar(select(s.peer_bindings.c.binding_json).where(s.peer_bindings.c.logical_id==first[0])))['object']
    active=await read(first[0],obj)
    check('actual_tls_target_shadow_range_before_cancel',active.status_code==206 and active.content==first[2][3:10])
    async with f.target.database.connect() as conn:
        deadline=await conn.scalar(select(func.max(s.read_leases.c.expires_at)).where(s.read_leases.c.member_id==task['target_id']))
    check('actual_target_reader_lease_is_durable',deadline is not None and deadline>time.time())
    stopped,_=await call('cancel-stop')
    check('native_target_stop_keeps_capacity_and_source',stopped['phase']=='stopped' and stopped['capacity_settled'] is False)
    for unit_id in units:await proof(unit_id)
    with_reader,_=await call('cancel-target',obj['unit_id'])
    check('actual_tls_existing_reader_blocks_shadow_removal',with_reader['phase']=='waiting_reader' and with_reader['deleted_count']==0)
    refused=await read(first[0],obj)
    check('actual_tls_new_read_refuses_after_cancel_intent',refused.status_code==409)
    await asyncio.sleep(max(0,deadline-time.time()+.1))
    check('reader_expired_with_real_clock',time.time()>=deadline)

    for unit_id in units:
        await proof(unit_id)
        try:await call('cancel-target',unit_id,permission=False)
        except TransferError as exc:check('missing_source_proof_refuses_'+unit_id,str(exc)=='migration_cancel_source_proof_invalid')
        else:raise AssertionError('Missing Source proof permitted native deletion')
        for _ in range(40):
            result,grant=await call('cancel-target',unit_id)
            if not restart_count and result['phase']=='objects' and result['deleted_count']==1:
                # The wire reply is deliberately discarded by the coordinator;
                # no Master acknowledgement is written before process restart.
                await restart_target();restart_count+=1
                await proof(unit_id)
                waiting,_=await call('cancel-target',unit_id)
                check('restarted_target_waits_for_real_quiet_interval',waiting['phase']=='waiting_low_traffic' and waiting['deleted_count']==1)
                await asyncio.sleep(30.1)
                await proof(unit_id)
                continue
            if result['phase']=='cancelled':
                async with f.target.database.connect() as conn:
                    unit=dict((await conn.execute(select(s.units).where(s.units.c.task_id==lease.task_id,s.units.c.unit_id==unit_id))).mappings().one())
                verify_receipt(result['receipt'],grant['payload']['target_key'],master_key,grant['payload'],selected(unit,grant['payload']),time.time())
                receipts[unit_id]=result['receipt'];break
        else:raise AssertionError('Actual HTTPS native Target rollback did not converge')
    check('independent_process_restart_reconciles_owned_shadow_deletion',restart_count==1)
    check('signed_unit_receipts_bind_all_cancelled_target_units',len(receipts)==len(units))
    check('all_original_source_plain_and_ciphertext_bytes_unchanged',all(path.read_bytes()==data for _,path,data in originals))
    check('all_known_target_shadows_removed',all(not (f.target.roots['media']/path.relative_to(f.source.roots['media'])).exists() for _,path,_ in originals))
    check('no_source_owner_quota_identity_or_fence_changed',await domain()==before)
    async with f.target.database.connect() as conn:
        check('target_has_no_live_encryption_descriptors',await conn.scalar(select(func.count()).select_from(crypto.encryption).where(crypto.encryption.c.descriptor_json.is_not(None)))==0)
        check('published_encryption_tombstones_are_retained',await conn.scalar(select(func.count()).select_from(crypto.encryption))==published_encrypted)
    # Signed HTTP nonces are durable across the real Target process restart.
    previous=next(row for row in reversed(inputs) if row['path'].endswith('/cancel-target'))
    later=f.target.roots['media']/first[1].relative_to(f.source.roots['media'])
    later.parent.mkdir(parents=True);later.write_bytes(b'later external material must survive terminal replay')
    await restart_target();restart_count+=1
    context=ssl.create_default_context(cafile=str(f.root/'ca.pem'))
    async with httpx.AsyncClient(verify=context,trust_env=False,timeout=5) as client:
        replay=await client.post(previous['url'],content=p.canonical(previous['value']),headers=previous['headers'])
        check('actual_tls_hmac_nonce_replay_refused',replay.status_code==401)
    replay,_=await call('cancel-target',obj['unit_id'],permission=False)
    check('terminal_receipt_replays_during_real_startup_quiet',replay['phase']=='cancelled' and replay['receipt']==receipts[obj['unit_id']])
    check('terminal_replay_never_deletes_later_external_material',later.read_bytes()==b'later external material must survive terminal replay')
    check('terminal_replay_keeps_all_quota_and_fences',await domain()==before)
    source_evidence={}
    if restart_source is not None:
        from tests.resource_cancel_source_native import finish as retain_source
        source_evidence=await retain_source(f,transport,traffic,lease,units,originals,receipts,restart_source,check,inputs,domain,restart_target=restart_target)
    return {**dict(native_target_shadow_cancel=True,actual_source_tls_proofs=True,actual_target_tls_cancellation=True,
        actual_reader_expiry=True,additional_target_restarts=restart_count,unit_receipt_count=len(receipts),
        source_files_removed=False,quota_refund=False,master_terminal_cancellation=False,public_post_publication_cancel=False,
        actual_http_inputs=inputs),**source_evidence}
