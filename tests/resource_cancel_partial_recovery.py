"""Independent Master recovery and genuine elapsed-hour mixed cancellation."""
import asyncio
import base64
import json
import os
import ssl
import sys
import time

import httpx
from sqlalchemy import func,select

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p


async def master_process(case,mode,check):
    f=case.f;await case.x.scheduler.stop()
    private=f.root/('master-recovery-'+mode+'.json');result=f.root/('master-result-'+mode+'.json')
    private.write_text(json.dumps(dict(url=f.master.database.url.render_as_string(hide_password=False),
        vault_key=base64.b64encode(f.master.vault_key).decode(),task_id=case.x.plan['plan_id'],mode=mode,result=str(result))))
    private.chmod(0o600)
    process=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.resource_cancel_master_process',str(private),
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        output,error=await asyncio.wait_for(process.communicate(),220)
        if process.returncode:raise AssertionError(error.decode(errors='replace')[-5000:])
        value=json.loads(result.read_text())
        check('independent_master_process_recovered_'+mode,value['pid']==process.pid and value['pid']!=os.getpid() and
            value['node_id']==f.master.store.node['node_id'] and value['epoch']>value['previous_epoch'])
        return value
    finally:
        if process.returncode is None:
            process.kill();await process.communicate()


async def open_reader(case,capability,check,inputs):
    f=case.f;logical,path,data=case.first
    check('precutover_source_capability_was_captured',capability is not None)
    client=httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(f.root/'ca.pem')),trust_env=False,
        timeout=httpx.Timeout(None,connect=5),limits=httpx.Limits(max_connections=1,max_keepalive_connections=0))
    request=client.build_request('GET',capability['url'],headers=capability['headers'])
    try:
        response=await client.send(request,stream=True)
        check('actual_source_reader_opens_with_precutover_capability',response.status_code==200 and
            int(response.headers.get('content-length','0'))==len(data))
        inputs.append(dict(method='GET',url=capability['url'],headers=capability['headers'],value=None))
        return client,response,capability['expires_at']
    except BaseException:await client.aclose();raise


async def finish(case,transport,heartbeat,wire_snapshot,reader,check):
    f=case.f;units=await case.units();first=units[0];started=time.monotonic();heartbeats=0
    check('original_committed_parent_has_real_one_hour_grace',first['cleanup_after']-first['committed_at']==3600)
    async def active():return (await wire_snapshot('source'))['traffic']['active_business_requests']
    check('actual_independent_source_socket_is_held',await active()==1)
    deadline=first['cleanup_after']+1
    while time.time()<deadline:
        await heartbeat();heartbeats+=2
        check_name='reader ended before original cleanup deadline'
        if await active()!=1:raise AssertionError(check_name)
        for _,path,data in case.x.resources:
            if not path.is_file() or path.stat().st_size!=len(data):raise AssertionError('Original removed before grace')
        task=await case.x.task();source_view=(await wire_snapshot('source'))['traffic'];target_view=(await wire_snapshot('target'))['traffic']
        print(json.dumps(dict(actual_hour_wait=True,remaining_seconds=round(deadline-time.time(),1),
            active_source_readers=1,authenticated_heartbeats=heartbeats,task_state=task['state'],task_error_code=task['error_code'],
            master_traffic=f.traffic.snapshot()['state'],source_traffic=source_view['state'],target_traffic=target_view['state'])),file=sys.stderr,flush=True)
        await asyncio.sleep(min(20,max(0,deadline-time.time())))
    check('actual_hour_elapsed_without_deadline_or_clock_edits',time.time()-first['committed_at']>=3600)
    check('held_reader_outlives_original_capability',time.time()>reader[2])
    async with f.source.database.connect() as conn:
        check('no_unexpired_capability_can_explain_reader_fence',await conn.scalar(select(func.count()).select_from(s.read_leases).where(
            s.read_leases.c.expires_at>int(time.time())))==0)
    await heartbeat()
    seen=False;statuses=set()
    async with asyncio.timeout(45):
        while not seen:
            await case.x.scheduler.tick();task=await case.x.task()
            status=(task['state'],task['error_code'])
            if status not in statuses:
                statuses.add(status);print(json.dumps(dict(reader_boundary_state=status,clock=time.time())),file=sys.stderr,flush=True)
            if task['state']=='failed':raise AssertionError(task['error_code'])
            seen=task['error_code']=='migration_waiting_reader'
            await asyncio.sleep(.1)
    check('actual_live_source_socket_blocks_cleanup_after_real_hour',case.first[1].read_bytes()==case.first[2] and await active()==1)
    await reader[1].aclose();await reader[0].aclose()
    async with asyncio.timeout(45):
        while True:
            observed=(await wire_snapshot('source'))['traffic']
            if observed['active_business_requests']==0 and observed['can_run']:break
            await asyncio.sleep(.2)
    check('real_socket_close_releases_native_reader_fence',True)
    recovery=await master_process(case,'terminal',check)
    check('mixed_cancellation_reaches_terminal_after_real_hour',recovery['state']=='cancelled')
    check('only_committed_source_replica_is_cleaned',not case.first[1].exists() and all(
        path.read_bytes()==data for _,path,data in case.x.resources[1:]))
    check('committed_target_bytes_are_preserved',
        (f.target.roots['media']/case.first[1].relative_to(f.source.roots['media'])).read_bytes()==case.first[2])
    for peer in (f.master,f.source,f.target):
        async with peer.database.connect() as conn:
            counts=[await conn.scalar(select(func.count()).select_from(table).where(
                table.c.owner_id==case.x.plan['plan_id'] if table is s.drains else table.c.task_id==case.x.plan['plan_id']))
                for table in (s.scopes,s.capacity,s.drains)]
            check('terminal_task_releases_exact_fences_'+peer.name,not any(counts))
            if peer is not f.master:check('native_terminal_receipt_retained_'+peer.name,
                await conn.scalar(select(s.peer_reservations.c.state))=='released')
    await case.histories();check('all_three_terminal_histories_are_readonly',True)
    return dict(actual_hour=True,whole_task_terminal=True,source_drain_released=True,held_reader_capability_expired=True,
        original_committed_at=first['committed_at'],original_cleanup_after=first['cleanup_after'],finished_at=time.time(),
        elapsed_cleanup_phase_seconds=round(time.monotonic()-started,4),actual_authenticated_heartbeats=heartbeats,
        master_recovery=recovery)
