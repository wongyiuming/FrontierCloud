"""Trusted TLS and independent peers for mixed partial unit settlement.

The default profile stops before the genuine one-hour cleanup deadline. Explicit
fixture flags add independent Master recovery and the elapsed-hour acceptance.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import ssl
import sys
import time
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
import httpx
from sqlalchemy import select,update

from app.services import resource_migration_schema as s
from app.services.business_traffic import Monitor
from app.services.federation import protocol as p,schema as fs
from app.services.federation.transport import Transport
from app.services.resource_cancel_private_receipts import FORMAT
from app.services.resource_transfer_protocol import CONTROL_BYTES
from tests.certificate_fixtures import TestCA
from tests.test_resource_cancel_partial import PartialCancellationTests


async def main():
    started=time.monotonic();case=PartialCancellationTests('runTest');await case.asyncSetUp();f=case.f
    children={};patches=[];checks=[];inputs=[];discarded=[];capability=None;reader=None;recoveries=[];hour_evidence={}
    actual_hour=os.environ.get('FC_PARTIAL_ACTUAL_HOUR')=='1'
    master_restart=actual_hour or os.environ.get('FC_PARTIAL_MASTER_PROCESS')=='1'
    def check(name,value):
        if not value:raise AssertionError(name)
        checks.append(name);print(json.dumps(dict(passed=name)),file=sys.stderr,flush=True)
    class ActualTransport(Transport):
        metadata_bytes=0
        discard=False
        def open(self):
            old=self.client;super().open()
            if old is None:
                async def record(request):
                    inputs.append(dict(method=request.method,url=str(request.url),path=request.url.raw_path.decode(),
                        headers={name:request.headers.get(name,'') for name in ('x-node-relationship','x-node-time','x-node-nonce','x-node-signature')},
                        value=json.loads(request.content) if request.content else None))
                self.client.event_hooks['request'].append(record)
        async def request(self,*args,**kwargs):
            nonlocal capability
            self.metadata_bytes+=len(p.canonical(kwargs.get('value')))
            result=await super().request(*args,**kwargs);self.metadata_bytes+=len(p.canonical(result))
            if actual_hour and capability is None and args[1].endswith('/verify-source') and hasattr(case,'first'):
                async with f.master.database.connect() as conn:
                    row=(await conn.execute(select(fs.global_media).where(fs.global_media.c.media_id==case.first[0]))).mappings().one()
                token=p.media_token(f.source.credential,f.source.relationship,f.master.store.node['node_id'],
                    f.source.store.node['node_id'],row['object_id'],case.first[0],int(time.time()))
                capability=dict(url=f.source.store.node['endpoint']+'/internal/v1/media/'+row['object_id'],
                    headers={'X-Media-Capability':token,'Origin':f.master.store.node['endpoint']},
                    expires_at=p.verify_media_token(f.source.credential,token,int(time.time()))['e']+5)
            if (self.discard and args[1].endswith('/cancel-target') and result.get('payload',{}).get('phase')=='cancelled' and
                    result['payload']['unit_id']==case.unit_id):
                self.discard=False;discarded.append(result['payload']['receipt'])
                raise httpx.ReadTimeout('Fixture discards the received private-parent cancellation response')
            return result
    transport=ActualTransport();ca,wrong=TestCA(),TestCA();chain,key=ca.pair();root=f.root
    for name,value in [('chain.pem',chain),('key.pem',key),('ca.pem',ca.root.public_bytes(serialization.Encoding.PEM)),
            ('wrong.pem',wrong.root.public_bytes(serialization.Encoding.PEM))]:(root/name).write_bytes(value)
    (root/'key.pem').chmod(0o600)
    pair=os.environ.get('FC_PARTIAL_TRANSPORT_PAIR','DR')
    if pair not in {'DD','DR','RD','RR'}:raise RuntimeError('Unknown fixture transport pair')
    async def stop_peer(side):
        process=children.get(side)
        if process is None:return
        await transport.close()
        if process.returncode is None:process.terminate()
        try:out,error=await asyncio.wait_for(process.communicate(),8)
        except TimeoutError:
            process.kill();out,error=await process.communicate();raise AssertionError('Peer did not shut down: '+error.decode(errors='replace')[-2000:])
        if process.returncode not in (0,-15):raise AssertionError(error.decode(errors='replace')[-4000:])
    async def start_peer(side,port):
        peer=getattr(f,side);private=root/('private-'+side+'.json')
        private.write_text(json.dumps(dict(url=str(peer.database.url),vault_key=base64.b64encode(peer.vault_key).decode(),
            roots={name:str(value) for name,value in peer.roots.items()},chain=str(root/'chain.pem'),key=str(root/'key.pem'),
            port=port,all_controls=True)));private.chmod(0o600)
        process=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.resource_transfer_native','--peer-child',str(private),
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        children[side]=process
        async with asyncio.timeout(15):
            while True:
                if process.returncode is not None:
                    out,error=await process.communicate();raise AssertionError(error.decode(errors='replace')[-4000:])
                try:
                    await transport.identity(peer.store.node['endpoint'],expected_id=peer.store.node['node_id'],
                        expected_key=p.public_key(peer.store.unseal(peer.store.node['private_key'])),role='Follower')
                    return process.pid
                except (httpx.HTTPError,p.ProtocolError):await asyncio.sleep(.05)
    async def heartbeat():
        for peer in (f.source,f.target):
            value=await transport.request(peer.store.node['endpoint'],'/internal/v1/heartbeat',method='POST',value={},
                credential=peer.credential,relation=peer.relationship)
            await f.master.store.heartbeat(peer.relationship,True,summary=value)
    async def wire_snapshot(side):
        peer=getattr(f,side);signed=await transport.request(peer.store.node['endpoint'],'/internal/v1/fixture-wire',method='POST',value={},
            credential=peer.credential,relation=peer.relationship)
        return p.verify(p.public_key(peer.store.unseal(peer.store.node['private_key'])),signed)
    try:
        patches=[patch.dict(os.environ,{'SSL_CERT_FILE':str(root/'ca.pem')}),patch.object(type(f),'now',property(lambda _self:time.time()),create=True)]
        for item in patches:item.start()
        f.traffic=Monitor();f.transport=transport;f.planner.transport=transport;f.planner.traffic=f.traffic
        await case.x.scheduler.stop();case.x.scheduler=case.x.new_scheduler()
        for side,letter,port in zip(('source','target'),pair,(18823,18824)):
            peer=getattr(f,side);origin=f'https://node.4399.com:{port}';mode='Direct' if letter=='D' else 'Relay'
            async with peer.database.begin() as conn:
                await conn.execute(update(fs.identity).values(endpoint=origin))
                await conn.execute(update(fs.relationships).where(fs.relationships.c.relationship_id==peer.relationship).values(mode=mode))
            peer.store.node['endpoint']=origin
            async with f.master.database.begin() as conn:
                await conn.execute(update(fs.relationships).where(fs.relationships.c.peer_id==peer.store.node['node_id']).values(peer_endpoint=origin,mode=mode))
                await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==peer.store.node['node_id']).values(transport=mode))
            await start_peer(side,port)
        first_target=children['target'].pid
        for name,origin,context in [('untrusted_ca_refused',f.source.store.node['endpoint'],ssl.create_default_context(cafile=str(root/'wrong.pem'))),
                ('wrong_hostname_refused','https://wrong.4399.com:18823',ssl.create_default_context(cafile=str(root/'ca.pem')))]:
            async with httpx.AsyncClient(verify=context,trust_env=False,timeout=3) as client:
                try:await client.get(origin+'/internal/v1/identity?challenge='+'a'*32)
                except httpx.ConnectError:check(name,True)
                else:raise AssertionError(name)
        await asyncio.sleep(30.1);check('real_startup_quiet_interval_observed',f.traffic.snapshot()['can_run'] and time.monotonic()-started>=30)
        await heartbeat();await case.prepare(first_bytes=b'held-real-source-reader'*(9*1024**2//23) if actual_hour else None)
        check('actual_tls_pipeline_reaches_committed_partial_and_never_started_units',True)
        transport.discard=True;restarted=False;last_heartbeat=time.monotonic()
        async with asyncio.timeout(180):
            while True:
                if time.monotonic()-last_heartbeat>=10:await heartbeat();last_heartbeat=time.monotonic()
                progress=await case.x.scheduler.tick();task=await case.x.task();units=await case.units()
                if task['state']=='failed':raise AssertionError(task['error_code'])
                if discarded and not restarted:
                    await stop_peer('target');await start_peer('target',18824)
                    check('target_process_restarted_after_received_receipt_was_discarded',children['target'].pid!=first_target)
                    restarted=True
                    await asyncio.sleep(30.1);await heartbeat();last_heartbeat=time.monotonic()
                    if master_restart:
                        from tests.resource_cancel_partial_recovery import master_process
                        recovery=await master_process(case,'units',check);recoveries.append(recovery)
                        inputs.extend(recovery['actual_http_inputs']);transport.metadata_bytes+=recovery['metadata_bytes']
                        task=await case.x.task();units=await case.units()
                if all(unit['state']=='cancelled' and unit['native_acknowledged']==1 for unit in units[1:]):break
                await asyncio.sleep(.01 if progress else .2)
        check('lost_private_receipt_recovered_with_original_native_receipt',len(discarded)==1 and restarted)
        for _,path,data in case.x.resources:check('source_original_preserved_'+path.name,path.read_bytes()==data)
        async with f.target.database.connect() as conn:
            rows={row['unit_id']:dict(row) for row in (await conn.execute(select(s.units))).mappings()}
            record=rows[case.unit_id]['publication']['target_cancel']['receipt'];payload=record['payload']
            check('durable_private_receipt_is_identical_after_independent_target_restart',record==discarded[0])
            check('private_receipt_preserves_logical_coverage_and_actual_partial_bytes',payload['format']==FORMAT and
                (payload['processed_count'],payload['deleted_count'],payload['absent_count'])==(3,2,1) and
                payload['deleted_bytes']==len(case.done[2])+case.partial_size<payload['total_bytes'])
            missing=rows[case.absent_unit]['publication']['target_cancel']['receipt']['payload']
            check('never_started_parent_reports_no_deleted_bytes',
                (missing['processed_count'],missing['deleted_count'],missing['deleted_bytes'],missing['absent_count'])==(1,0,0,1))
        check('private_parts_are_removed_without_removing_source_or_committed_media',not case.part.exists() and
            (f.target.roots['media']/case.first[1].relative_to(f.source.roots['media'])).read_bytes()==case.first[2])
        for peer in (f.master,f.target):
            async with peer.database.connect() as conn:
                check('logical_reservations_refunded_exactly_'+peer.name,await conn.scalar(select(s.capacity.c.reserved_bytes))==0)
        check('original_cleanup_deadline_is_real_and_unchanged',units[0]['cleanup_after']-units[0]['committed_at']==3600 and time.time()<units[0]['cleanup_after'])
        await case.x.scheduler.stop()
        async with f.master.database.connect() as conn:
            check('task_remains_cancelling_and_source_drain_is_retained_before_actual_hour',
                await conn.scalar(select(s.tasks.c.state))=='cancelling' and await conn.scalar(select(s.drains.c.owner_id)) is not None)
        if actual_hour:
            from tests.resource_cancel_partial_recovery import open_reader,finish
            reader=await open_reader(case,capability,check,inputs)
            hour_evidence=await finish(case,transport,heartbeat,wire_snapshot,reader,check)
            reader=None;recoveries.append(hour_evidence['master_recovery'])
            inputs.extend(recoveries[-1]['actual_http_inputs']);transport.metadata_bytes+=recoveries[-1]['metadata_bytes']
        wire={}
        for side in ('source','target'):
            wire[side]=await wire_snapshot(side)
        check('independent_source_and_target_processes_are_live',wire['source']['pid']==children['source'].pid and
            wire['target']['pid']==children['target'].pid and children['source'].pid!=children['target'].pid)
        check('source_data_plane_carried_the_actual_payload',wire['source']['data_sent']>=case.partial_size+len(case.done[2])+len(case.first[2]))
        check('master_carried_only_bounded_metadata',transport.metadata_bytes<wire['source']['data_sent']//2 and
            all(len(p.canonical(value['value']))<=CONTROL_BYTES for value in inputs))
        samples=[]
        def usage():return int(dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())['usage_usec'])
        for _ in range(5):
            previous,wall=usage(),time.monotonic();await asyncio.sleep(1);samples.append(round((usage()-previous)/10000/(time.monotonic()-wall),3))
        check('five_whole_cgroup_quiescent_samples_under_twenty_percent',all(value<20 for value in samples))
        quota,period=Path('/sys/fs/cgroup/cpu.max').read_text().split();memory=int(Path('/sys/fs/cgroup/memory.max').read_text())
        events={name:int(value) for name,value in (line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())}
        check('one_cpu_512mib_parent_and_both_peers_without_oom',quota==period and memory==512*1024**2 and not events.get('oom') and not events.get('oom_kill'))
        print(json.dumps(dict(format='frontiercloud-python-partial-cancel-tls',version=1,checks=checks,count=len(checks),transport_pair=pair,
            actual_https=True,tls_verified=True,independent_peers=True,target_process_restart=True,lost_target_receipt_recovered=True,
            master_independent_process_restart=master_restart,whole_task_terminal=actual_hour,source_drain_released=actual_hour,
            actual_hour=actual_hour,master_recoveries=recoveries,hour_evidence=hour_evidence,
            actual_http_inputs=inputs,wire=wire,master_metadata_bytes=transport.metadata_bytes,media_decrypted=False,cpu_samples=samples,
            cpu_quota=int(quota),cpu_period=int(period),memory_limit=memory,memory_events=events,
            elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    except BaseException as error:
        failure=dict(format='frontiercloud-python-partial-cancel-tls-failure',version=1,passed=False,
            error=type(error).__name__,actual_hour_attempted=actual_hour,actual_hour_completed=False,
            checks=checks,actual_http_inputs=inputs,master_recoveries=recoveries)
        try:
            task=await case.x.task();failure['task']={name:task[name] for name in ('state','revision','error_code','updated_at','committed_bytes')}
            failure['units']=[{name:unit[name] for name in ('unit_id','state','native_acknowledged','committed_at','cleanup_after')} for unit in await case.units()]
            failure['master_traffic']=f.traffic.snapshot();failure['clock']=time.time()
            failure['fixture_clock']=f.now;candidate=await case.x.scheduler._candidate()
            failure['candidate']=None if candidate is None else {name:candidate[name] for name in ('task_id','state','error_code')}
            async with f.master.database.connect() as conn:
                failure['runners']=[dict(row) for row in (await conn.execute(select(s.runners.c.epoch,s.runners.c.expires_at))).mappings()]
            failure['wire']={}
            for side in ('source','target'):
                async with asyncio.timeout(3):failure['wire'][side]=await wire_snapshot(side)
        except Exception as diagnostic:failure['diagnostic_error']=type(diagnostic).__name__
        print(json.dumps(failure,sort_keys=True),flush=True)
        raise
    finally:
        if reader is not None:await reader[1].aclose();await reader[0].aclose()
        await transport.close()
        for side in ('target','source'):await stop_peer(side)
        # Restore the fixture's original clock only after releasing its runner.
        await case.x.scheduler.stop()
        for item in reversed(patches):item.stop()
        await case.asyncTearDown()


if __name__=='__main__':asyncio.run(main())
