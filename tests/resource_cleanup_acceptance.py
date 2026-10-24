"""Actual elapsed-hour cleanup over trusted TLS; never edits a clock or deadline."""
import asyncio
import json
import ssl
import sys
import time

import httpx
from sqlalchemy import func,select

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cleanup_master import Cleanup
from app.services.resource_finalize_master import Finalization


class HeldReader:
    def __init__(self,client,response,expires_at):self.client,self.response,self.expires_at=client,response,expires_at

    @classmethod
    async def open(cls,f,original,check):
        logical,path,data=original
        async with f.master.database.connect() as conn:
            row=(await conn.execute(select(fs.global_media).where(fs.global_media.c.media_id==logical))).mappings().one()
        token=p.media_token(f.source.credential,f.source.relationship,f.master.store.node['node_id'],
            f.source.store.node['node_id'],row['object_id'],logical,int(time.time()))
        expires=p.verify_media_token(f.source.credential,token,int(time.time()))['e']+5
        client=httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(f.root/'ca.pem')),trust_env=False,
            timeout=httpx.Timeout(None,connect=5),limits=httpx.Limits(max_connections=1,max_keepalive_connections=0))
        try:
            request=client.build_request('GET',f.source.store.node['endpoint']+'/internal/v1/media/'+row['object_id'],
                headers={'X-Media-Capability':token,'Origin':f.master.store.node['endpoint']})
            response=await client.send(request,stream=True)
            check('real_source_media_reader_opens_before_cutover',response.status_code==200 and
                int(response.headers.get('content-length','0'))==len(data))
            await asyncio.sleep(.25)
            check('real_source_response_is_still_active_under_socket_backpressure',
                f.source.traffic.snapshot()['active_business_requests']==1)
            return cls(client,response,expires)
        except BaseException:await client.aclose();raise

    async def close(self):
        await self.response.aclose();await self.client.aclose()


async def finish(f,transport,master_traffic,lease,units,originals,held,check):
    holder=[lease];heartbeats=0;started=time.monotonic()

    async def refresh():
        nonlocal heartbeats
        while True:
            for peer in (f.source,f.target):
                summary=await transport.request(peer.store.node['endpoint'],'/internal/v1/heartbeat',method='POST',
                    value={},credential=peer.credential,relation=peer.relationship)
                if summary.get('protocol')!=p.PROTOCOL_VERSION:raise AssertionError('heartbeat protocol changed')
                await f.master.store.heartbeat(peer.relationship,True,summary=summary);heartbeats+=1
            await cleanup.t.journal.renew(holder[0],ttl=60)
            await asyncio.sleep(20)

    cleanup=Cleanup(f.master.store,transport,traffic=master_traffic)
    refresh_task=asyncio.create_task(refresh(),name='native-hour-real-heartbeats')
    try:
        async with f.master.database.connect() as conn:
            records=[dict(row) for row in (await conn.execute(select(s.units).where(s.units.c.task_id==lease.task_id))).mappings()]
        check('immutable_native_grace_is_one_hour',all(row['cleanup_after']-row['committed_at']==3600 for row in records))
        holder[0],early=await cleanup.advance(holder[0],units[0])
        check('cleanup_refuses_before_original_real_deadline',early['phase']=='waiting_grace')
        deadline=max(row['cleanup_after'] for row in records)+1
        while time.time()<deadline:
            if refresh_task.done():await refresh_task;raise AssertionError('heartbeat task unexpectedly stopped')
            if f.source.traffic.snapshot()['active_business_requests']!=1:raise AssertionError('held real reader ended before grace')
            print(json.dumps(dict(real_hour_wait=True,remaining_seconds=round(deadline-time.time(),1),
                active_source_readers=1,actual_authenticated_heartbeats=heartbeats)),file=sys.stderr,flush=True)
            await asyncio.sleep(min(30,max(0,deadline-time.time())))
        check('actual_full_hour_elapsed_since_every_committed_parent',all(time.time()-row['committed_at']>=3600 for row in records))
        check('held_source_read_outlives_original_capability_deadline',time.time()>held.expires_at)
        async with f.source.database.connect() as conn:
            live=await conn.scalar(select(func.count()).select_from(s.read_leases).where(s.read_leases.c.expires_at>int(time.time())))
        check('source_has_no_unexpired_capability_lease_at_cleanup_boundary',live==0)
        for _ in range(35):
            holder[0],result=await cleanup.advance(holder[0],units[0])
            if result['phase']=='waiting_reader':break
            await asyncio.sleep(.1)
        else:raise AssertionError('actual open Source socket did not block cleanup after full hour')
        check('actual_open_http_reader_blocks_cleanup_after_capability_expiry',result['deleted_count']==0 and
            all(path.read_bytes()==data for _,path,data in originals))
        await held.close()
        async with asyncio.timeout(45):
            while not (f.source.traffic.snapshot()['readers_clear'] and f.source.traffic.snapshot()['can_run']):await asyncio.sleep(.2)
        check('actual_socket_close_releases_active_reader_before_cleanup',f.source.traffic.snapshot()['active_business_requests']==0)
        for unit_id in units:
            for _ in range(50):
                holder[0],result=await cleanup.advance(holder[0],unit_id)
                if result['source_cleaned']:break
                await asyncio.sleep(.05)
            else:raise AssertionError('native Source cleanup did not converge')
        check('all_planned_native_source_replicas_removed_after_actual_hour',all(not path.exists() for _,path,_ in originals))
        check('target_plain_and_ciphertext_originals_preserved_after_cleanup',all(
            (f.target.roots['media']/path.relative_to(f.source.roots['media'])).read_bytes()==data for _,path,data in originals))
        refresh_task.cancel()
        try:await refresh_task
        except asyncio.CancelledError:pass
        finalizer=Finalization(f.master.store,transport,traffic=master_traffic)
        for _ in range(50):
            result=await finalizer.advance(holder[0])
            if result['task_completed']:break
            await asyncio.sleep(.05)
        else:raise AssertionError('native final censuses did not converge')
        for peer in (f.master,f.source,f.target):
            async with peer.database.connect() as conn:
                remaining=[]
                for table in (s.scopes,s.capacity,s.drains):
                    remaining.append(await conn.scalar(select(func.count()).select_from(table).where(
                        table.c.owner_id==lease.task_id if table is s.drains else table.c.task_id==lease.task_id)))
                check('actual_hour_releases_only_completed_task_fences_'+peer.name,all(value==0 for value in remaining))
                if peer.name!='master':
                    check('actual_hour_native_terminal_receipt_retained_'+peer.name,
                        await conn.scalar(select(s.peer_reservations.c.state))=='released')
        async with f.master.database.connect() as conn:
            check('actual_hour_task_completed_only_after_both_native_receipts',
                await conn.scalar(select(s.tasks.c.state))=='completed' and len(result['completion']['receipts'])==2)
        return dict(real_cleanup_grace=True,source_cleanup=True,task_completion=True,
            elapsed_cleanup_phase_seconds=round(time.monotonic()-started,4),actual_authenticated_heartbeats=heartbeats,
            original_committed_at=min(row['committed_at'] for row in records),last_original_cleanup_after=max(row['cleanup_after'] for row in records),
            finished_at=time.time(),held_reader_capability_expired=True)
    finally:
        refresh_task.cancel()
        try:await refresh_task
        except asyncio.CancelledError:pass
        await held.close()
