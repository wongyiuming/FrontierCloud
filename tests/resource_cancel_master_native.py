"""Real TLS coordinator inputs, native process recovery and live read protection."""
import asyncio
import json
import ssl
import time
import uuid

import httpx
from sqlalchemy import func,select,update

from app.services import media_crypto_schema as crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_master import PublicationCancellation
from app.services.resource_transfer_protocol import TransferError


async def finish(f,transport,traffic,lease,units,originals,restart_source,restart_target,check,inputs):
    async with f.master.database.begin() as conn:
        task=(await conn.execute(select(s.tasks).where(s.tasks.c.task_id==lease.task_id))).mappings().one()
        if task['state']!='committing' or task['committed_bytes']!=0:raise AssertionError('Expected genuinely uncommitted published parents')
        await conn.execute(update(s.tasks).where(s.tasks.c.task_id==lease.task_id).values(state='cancelling',revision=task['revision']+1))
        await conn.execute(update(s.runners).where(s.runners.c.task_id==lease.task_id).values(expires_at=0))
    coordinator=PublicationCancellation(f.master.store,transport,traffic=traffic)
    lease=await coordinator.t.journal.claim(lease.task_id,uuid.uuid4().hex,ttl=60)
    async def heartbeat():
        for peer in (f.source,f.target):
            summary=await transport.request(peer.store.node['endpoint'],'/internal/v1/heartbeat',method='POST',value={},
                credential=peer.credential,relation=peer.relationship)
            await f.master.store.heartbeat(peer.relationship,True,summary=summary)
    async def domain():
        result={}
        for peer in (f.master,f.source,f.target):
            async with peer.database.connect() as conn:
                result[peer.name]={table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in
                    ((fs.identity,fs.global_media,crypto.encryption) if peer.name!='target' else (fs.identity,))}
        return result
    before=await domain();await restart_source();await restart_target();source_restarts=1;target_restarts=1
    old_request=transport.request;lost=set();last_lost=None
    async def lose(origin,path,**kwargs):
        nonlocal last_lost
        result=await old_request(origin,path,**kwargs)
        if path.endswith(('/cancel-target','/cancel-source-retain')) and result['payload']['phase'] in {'cancelled','retained'} and path not in lost:
            lost.add(path);last_lost=path;raise httpx.ReadTimeout('Fixture discarded a received terminal peer reply')
        return result
    transport.request=lose
    restarted_page=False;reader=None;reader_deadline=None;reader_wait=False;quiet_wait=False;phases=[]
    async def read(obj):
        token=p.media_token(f.target.credential,f.target.relationship,f.master.store.node['node_id'],f.target.store.node['node_id'],
            obj['source_object'],obj['logical_id'],int(time.time())-p.TOKEN_SECONDS+20)
        context=ssl.create_default_context(cafile=str(f.root/'ca.pem'))
        async with httpx.AsyncClient(verify=context,trust_env=False,timeout=5) as client:
            return await client.get(f.target.store.node['endpoint']+'/internal/v1/media/'+obj['source_object'],
                headers={'X-Media-Capability':token,'Range':'bytes=3-9','Origin':f.master.store.node['endpoint']})
    try:
        for _ in range(240):
            await heartbeat()
            try:lease,result=await coordinator.advance(lease)
            except httpx.ReadTimeout:
                if last_lost.endswith('/cancel-target'):await restart_target();target_restarts+=1
                else:await restart_source();source_restarts+=1
                coordinator=PublicationCancellation(f.master.store,transport,traffic=traffic)
                continue
            except TransferError as exc:
                if not str(exc).endswith(('waiting_low_traffic','low_traffic')):raise
                quiet_wait=True;await coordinator.t.journal.renew(lease,ttl=60);await asyncio.sleep(30.1);continue
            phases.append(result['phase'])
            if result['phase']=='source_proof_objects' and not restarted_page:
                await restart_source();source_restarts+=1;restarted_page=True
                coordinator=PublicationCancellation(f.master.store,transport,traffic=traffic);continue
            if result['phase']=='source_proof_verified' and reader is None:
                async with f.target.database.connect() as conn:
                    reader=json.loads(await conn.scalar(select(s.peer_bindings.c.binding_json).where(
                        s.peer_bindings.c.unit_id==result['unit_id']).order_by(s.peer_bindings.c.logical_id).limit(1)))['object']
                response=await read(reader)
                original=next(data for logical,_,data in originals if logical==reader['logical_id'])
                check('actual_pipeline_target_range_before_cancel_intent',response.status_code==206 and response.content==original[3:10])
                async with f.target.database.connect() as conn:reader_deadline=await conn.scalar(select(func.max(s.read_leases.c.expires_at)))
            if result['phase']=='target_waiting_reader' and reader_deadline:
                check('coordinator_retains_live_target_reader_and_shadows',time.time()<reader_deadline)
                refused=await read(reader);check('coordinator_cancel_intent_blocks_new_target_reads',refused.status_code==409)
                reader_wait=True;await coordinator.t.journal.renew(lease,ttl=60);await asyncio.sleep(max(0,reader_deadline-time.time()+.1));continue
            if result['phase']=='source_waiting_reader':
                async with f.source.database.connect() as conn:deadline=await conn.scalar(select(func.max(s.read_leases.c.expires_at)).where(s.read_leases.c.purpose=='migration'))
                await coordinator.t.journal.renew(lease,ttl=60);await asyncio.sleep(max(.1,(deadline or time.time())-time.time()+.1));continue
            if result['phase'].endswith('waiting_low_traffic'):
                quiet_wait=True;await coordinator.t.journal.renew(lease,ttl=60);await asyncio.sleep(30.1);continue
            if result['units_settled']:break
        else:raise AssertionError('Real TLS coordinator did not settle every parent')
    finally:transport.request=old_request
    check('pipeline_joins_actual_target_reader_before_removal',reader_wait)
    check('pipeline_repeats_actual_quiet_after_native_restart',quiet_wait and restarted_page)
    check('pipeline_recovers_received_target_and_source_receipts_after_restart',len(lost)==2 and source_restarts==3 and target_restarts==2)
    check('pipeline_preserves_source_bytes',all(path.read_bytes()==data for _,path,data in originals))
    check('pipeline_preserves_business_crypto_and_node_keys',await domain()==before)
    check('pipeline_removes_only_uncommitted_target_shadows',all(not (f.target.roots['media']/path.relative_to(f.source.roots['media'])).exists() for _,path,_ in originals))
    for peer in (f.master,f.source,f.target):
        async with peer.database.connect() as conn:
            check('pipeline_releases_unit_scopes_'+peer.name,await conn.scalar(select(func.count()).select_from(s.scopes))==0)
            check('pipeline_zero_owned_reserved_'+peer.name,await conn.scalar(select(s.capacity.c.reserved_bytes))==0)
            check('pipeline_native_units_settled_'+peer.name,await conn.scalar(select(func.count()).select_from(s.units).where(s.units.c.state=='cancelled',s.units.c.native_acknowledged==1))==len(units))
            if peer.name!='target':check('pipeline_keeps_source_drain_'+peer.name,await conn.scalar(select(func.count()).select_from(s.drains))==1)
    async with f.master.database.connect() as conn:
        task=(await conn.execute(select(s.tasks))).mappings().one()
        check('pipeline_keeps_whole_task_active',task['state']=='cancelling' and task['committed_bytes']==0 and result['task_completed'] is False)
    stop_inputs=[row for row in inputs if row['path'].endswith(('/cancel-source-stop','/cancel-stop'))]
    check('pipeline_durable_stop_receipts_prevent_scan_reset',sum(row['path'].endswith('/cancel-source-stop') for row in stop_inputs)<=2 and
        sum(row['path'].endswith('/cancel-stop') for row in stop_inputs)==1)
    return dict(master_native_unit_cancellation=True,coordinator_stages=sorted(set(phases)),source_process_restarts=source_restarts,
        additional_target_restarts=target_restarts,quota_refund=True,source_files_removed=False,master_terminal_cancellation=False,
        full_cancellation_acceptance=False,public_post_publication_cancel=False,actual_http_inputs=inputs)
