"""Real elapsed-hour mixed cleanup; clocks and recorded deadlines stay intact."""
import asyncio
import json
import sys
import time
import uuid

from sqlalchemy import func,select,update

from app.services import resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.resource_cancel_master import PublicationCancellation
from app.services.resource_cleanup_master import Cleanup
from app.services.resource_cutover_master import Cutover
from app.services.resource_transfer_protocol import TransferError
from tests.resource_cleanup_acceptance import HeldReader


async def finish(f,transport,traffic,lease,units,originals,check,inputs):
    async def heartbeat():
        for peer in (f.source,f.target):
            value=await transport.request(peer.store.node['endpoint'],'/internal/v1/heartbeat',method='POST',value={},
                credential=peer.credential,relation=peer.relationship)
            await f.master.store.heartbeat(peer.relationship,True,summary=value)
    async with f.master.database.connect() as conn:
        committed_unit=await conn.scalar(select(s.objects.c.unit_id).where(s.objects.c.task_id==lease.task_id,s.objects.c.logical_id==originals[0][0]))
        committed_ids=set((await conn.execute(select(s.objects.c.logical_id).where(s.objects.c.task_id==lease.task_id,s.objects.c.unit_id==committed_unit))).scalars())
    committed=[row for row in originals if row[0] in committed_ids];retained=[row for row in originals if row[0] not in committed_ids]
    check('mixed_real_hour_has_committed_and_retained_parents',bool(committed) and bool(retained))
    held=await HeldReader.open(f,originals[0],check);refresh_task=None;heartbeats=0;started=time.monotonic()
    cutover=Cutover(f.master.store,transport,traffic=traffic)
    try:
        await heartbeat()
        lease,record=await cutover.commit(lease,committed_unit);commit=record['commit']
        for _ in range(4):
            if (await cutover.acknowledge_one(lease,committed_unit))['native_acknowledged']:break
        else:raise AssertionError('Mixed committed parent did not receive both native acknowledgements')
        async with f.master.database.begin() as conn:
            task=(await conn.execute(select(s.tasks))).mappings().one()
            await conn.execute(update(s.tasks).values(state='cancelling',revision=task['revision']+1))
            await conn.execute(update(s.runners).values(expires_at=0))
        coordinator=PublicationCancellation(f.master.store,transport,traffic=traffic)
        lease=await coordinator.t.journal.claim(lease.task_id,uuid.uuid4().hex,ttl=60)
        for _ in range(200):
            await heartbeat()
            try:lease,result=await coordinator.advance(lease)
            except TransferError as exc:
                if not str(exc).endswith(('waiting_low_traffic','low_traffic')):raise
                await coordinator.t.journal.renew(lease,ttl=60);await asyncio.sleep(30.1);continue
            if result['phase']=='source_waiting_reader':
                async with f.source.database.connect() as conn:deadline=await conn.scalar(select(func.max(s.read_leases.c.expires_at)).where(s.read_leases.c.purpose=='migration'))
                await coordinator.t.journal.renew(lease,ttl=60);await asyncio.sleep(max(.1,(deadline or time.time())-time.time()+.1))
            elif result['phase'].endswith('waiting_low_traffic'):
                await coordinator.t.journal.renew(lease,ttl=60);await asyncio.sleep(30.1)
            if result['units_settled']:break
        else:raise AssertionError('Mixed cancellation did not settle its uncommitted parents')
        check('mixed_retained_originals_unchanged_after_cancellation',all(path.read_bytes()==data for _,path,data in retained))
        check('mixed_committed_source_replicas_still_present_before_original_deadline',all(path.read_bytes()==data for _,path,data in committed))
        cleanup=Cleanup(f.master.store,transport,traffic=traffic);holder=[lease]
        async def refresh():
            nonlocal heartbeats
            while True:
                await heartbeat();heartbeats+=2;await cleanup.t.journal.renew(holder[0],ttl=60);await asyncio.sleep(20)
        refresh_task=asyncio.create_task(refresh(),name='mixed-hour-authenticated-heartbeats')
        holder[0],early=await cleanup.advance(holder[0],committed_unit)
        check('mixed_cleanup_respects_original_hour_after_cancellation',early['phase']=='waiting_grace' and commit['cleanup_after']-commit['committed_at']==3600)
        deadline=commit['cleanup_after']+1
        while time.time()<deadline:
            if refresh_task.done():await refresh_task;raise AssertionError('Mixed heartbeat keeper stopped')
            if f.source.traffic.snapshot()['active_business_requests']!=1:raise AssertionError('Held Source socket ended before the original grace')
            print(json.dumps(dict(mixed_real_hour_wait=True,remaining_seconds=round(deadline-time.time(),1),
                actual_authenticated_heartbeats=heartbeats)),file=sys.stderr,flush=True)
            await asyncio.sleep(min(30,max(0,deadline-time.time())))
        check('mixed_actual_hour_elapsed_without_editing_deadline',time.time()-commit['committed_at']>=3600)
        check('mixed_source_socket_outlives_its_capability',time.time()>held.expires_at)
        async with f.source.database.connect() as conn:
            check('mixed_no_unexpired_source_capability_leases_at_cleanup',await conn.scalar(select(func.count()).select_from(s.read_leases).where(
                s.read_leases.c.expires_at>int(time.time())))==0)
        for _ in range(40):
            holder[0],result=await cleanup.advance(holder[0],committed_unit)
            if result['phase']=='waiting_reader':break
            await asyncio.sleep(.05)
        else:raise AssertionError('Live Source socket did not block mixed cleanup')
        check('mixed_live_socket_blocks_replica_unlink_after_capability_expiry',result['deleted_count']==0 and all(path.exists() for _,path,_ in committed))
        await held.close()
        async with asyncio.timeout(45):
            while not (f.source.traffic.snapshot()['can_run'] and f.source.traffic.snapshot()['readers_clear']):await asyncio.sleep(.2)
        for _ in range(60):
            holder[0],result=await cleanup.advance(holder[0],committed_unit)
            if result['source_cleaned']:break
            await asyncio.sleep(.05)
        else:raise AssertionError('Mixed committed Source cleanup did not converge')
        check('mixed_only_committed_source_replicas_removed',all(not path.exists() for _,path,_ in committed) and all(path.read_bytes()==data for _,path,data in retained))
        check('mixed_committed_target_originals_preserved',all((f.target.roots['media']/path.relative_to(f.source.roots['media'])).read_bytes()==data for _,path,data in committed))
        check('mixed_cancelled_target_shadows_remain_absent',all(not (f.target.roots['media']/path.relative_to(f.source.roots['media'])).exists() for _,path,_ in retained))
        async with f.master.database.connect() as conn:
            task=(await conn.execute(select(s.tasks))).mappings().one()
            check('mixed_cleanup_retains_task_state_committed_bytes_and_source_drain',task['state']=='cancelling' and task['committed_bytes']==sum(len(data) for _,_,data in committed) and
                await conn.scalar(select(func.count()).select_from(s.drains))==1)
            check('mixed_cleanup_does_not_claim_empty_source',set((await conn.execute(select(fs.global_media.c.storage_member_id))).scalars())=={
                f.source.store.node['node_id'],f.target.store.node['node_id']})
        return dict(mixed_source_cleanup=True,real_cleanup_grace=True,source_cleanup=True,task_completion=False,master_terminal_cancellation=False,
            full_cancellation_acceptance=False,cancelled_source_originals_preserved=True,actual_authenticated_heartbeats=heartbeats,
            actual_http_inputs=inputs,original_committed_at=commit['committed_at'],original_cleanup_after=commit['cleanup_after'],
            finished_at=time.time(),elapsed_mixed_phase_seconds=round(time.monotonic()-started,4),ordinary_source_socket_outlived_capability=True)
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            try:await refresh_task
            except asyncio.CancelledError:pass
        await held.close()
