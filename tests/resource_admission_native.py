"""Verified TLS preparation and actual child-process Master recovery, no transfer."""
import asyncio
import base64
import contextvars
import json
import os
from pathlib import Path
import resource
import ssl
import sys
import time
import uuid
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
import httpx
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
import uvicorn

from app.api import internal_nodes, internal_resource_inventory as inventory_api
from app.api import internal_resource_observation as observation_api, internal_resource_registry as registry_api
from app.api import internal_resource_reservations as reservation_api
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_crypto as crypto
from app.services.business_traffic import Monitor
from app.services.federation import schema as fs
from app.services.federation.state import State
from app.services.federation.transport import Transport
from app.services.resource_migration_admission import Admission
from app.services.resource_migration_journal import Journal
from tests.certificate_fixtures import TestCA
from tests import test_resource_migration_admission as fixtures


async def child(path):
    private=json.loads(Path(path).read_text());database=create_sqlite_engine(make_url(private['url']))
    transport=Transport()
    try:
        store=State(database,base64.b64decode(private['vault_key']));await store.initialize()
        traffic=Monitor();traffic._quiet_since=time.monotonic()-60
        service=Admission(store,transport,traffic=traffic)
        with (patch.object(crypto,'open_premaster',side_effect=AssertionError('child opened premaster')),
              patch.object(crypto.Manager,'file_key',side_effect=AssertionError('child decrypted media'))):
            result=await service.prepare(private['task_id'],private['review_sha256'],'native-child')
        assert result['state']=='prepared' and not result['execution_authorized']
        print(json.dumps(dict(child_process_recovery=True,state=result['state'],objects=result['object_count'])))
    finally:
        await transport.close();await database.dispose()


async def main():
    started=time.monotonic();fixture=fixtures.MigrationAdmissionTests('runTest');await fixture.asyncSetUp()
    f=fixture.f;checks=[];wire=dict(requests=0,received_metadata_bytes=0,sent_metadata_bytes=0)
    selected=contextvars.ContextVar('admission_peer');servers=[];tasks=[];transport=Transport()
    def check(name,value):
        if not value:raise AssertionError(name)
        checks.append(name);print(json.dumps(dict(passed=name)),file=sys.stderr,flush=True)
    class Proxy:
        def __init__(self,name):self.name=name
        def __getattr__(self,name):return getattr(getattr(selected.get(),self.name),name)
    class Bound:
        def __init__(self,app,peer):self.app,self.peer=app,peer
        async def __call__(self,scope,receive,send):
            token=selected.set(self.peer)
            if scope['type']=='http':wire['requests']+=1
            async def received():
                value=await receive();wire['received_metadata_bytes']+=len(value.get('body',b''));return value
            async def sent(value):
                wire['sent_metadata_bytes']+=len(value.get('body',b''));await send(value)
            try:await self.app(scope,received,sent)
            finally:selected.reset(token)
    ca,wrong=TestCA(),TestCA();chain,key=ca.pair();root=f.root
    for name,value in (('chain.pem',chain),('key.pem',key),('ca.pem',ca.root.public_bytes(serialization.Encoding.PEM)),
                       ('wrong.pem',wrong.root.public_bytes(serialization.Encoding.PEM))):
        (root/name).write_bytes(value)
    (root/'key.pem').chmod(0o600)
    patches=[patch.object(internal_nodes,'state',Proxy('store')),patch.object(inventory_api,'state',Proxy('store')),
        patch.object(inventory_api,'inventory',Proxy('inventory')),patch.object(observation_api,'observer',Proxy('observer')),
        patch.object(registry_api,'registry',Proxy('registry')),patch.object(reservation_api,'reservations',Proxy('reservations')),
        patch.object(internal_nodes.settings,'TLS_ENABLED',True),patch.dict(os.environ,{'SSL_CERT_FILE':str(root/'ca.pem')})]
    try:
        for item in patches:item.start()
        for port,peer in enumerate((f.source,f.target),18783):
            origin=f'https://node.4399.com:{port}';peer.store.node['endpoint']=origin
            async with peer.database.begin() as conn:await conn.execute(update(fs.identity).values(endpoint=origin))
            async with f.master.database.begin() as conn:
                await conn.execute(update(fs.relationships).where(fs.relationships.c.peer_id==peer.store.node['node_id']).values(peer_endpoint=origin))
            app=FastAPI()
            for router in (internal_nodes.router,inventory_api.router,observation_api.router,registry_api.router,reservation_api.router):
                app.include_router(router)
            server=uvicorn.Server(uvicorn.Config(Bound(app,peer),host='127.0.0.1',port=port,ssl_certfile=str(root/'chain.pem'),
                ssl_keyfile=str(root/'key.pem'),lifespan='off',access_log=False,log_level='error',workers=1))
            servers.append(server);tasks.append(asyncio.create_task(server.serve()))
        async with asyncio.timeout(5):
            while not all(server.started for server in servers):
                for job in tasks:
                    if job.done():await job;raise AssertionError('native TLS stopped')
                await asyncio.sleep(.01)
        for name,origin,context in (
            ('untrusted_ca_refused','https://node.4399.com:18783',ssl.create_default_context(cafile=str(root/'wrong.pem'))),
            ('wrong_hostname_refused','https://wrong.4399.com:18783',ssl.create_default_context(cafile=str(root/'ca.pem')))):
            async with httpx.AsyncClient(verify=context,trust_env=False,timeout=3) as client:
                try:await client.get(origin+'/internal/v1/identity',params={'challenge':'a'*32})
                except httpx.ConnectError:check(name,True)
                else:raise AssertionError(name)
        originals=[await f.media(b'ID3'+bytes(2*1024*1024)),await f.media(b'ciphertext original'*3000,
            path='music/second/private.mp3',encrypted=True)]
        f.planner.transport=transport
        ready=await f.plan();check('fresh_full_preview_over_verified_https',ready['state']=='ready')
        service=Admission(f.master.store,transport,preview_epoch=f.planner.epoch,traffic=f.traffic)
        original_request=transport.request;lost=False
        async def lose_once(origin,path,**kwargs):
            nonlocal lost
            value=await original_request(origin,path,**kwargs)
            if origin==f.target.store.node['endpoint'] and path.endswith('/reservations/begin') and not lost:
                lost=True;raise httpx.ReadError('lost successful target acknowledgement')
            return value
        with patch.object(transport,'request',side_effect=lose_once):
            try:await service.prepare(ready['plan_id'],ready['review_sha256'],'native')
            except httpx.ReadError:check('lost_target_acknowledgement_preserves_incomplete_task',True)
            else:raise AssertionError('fault not exercised')
        check('master_and_target_reserve_once_before_restart',await fixture.quota()==(ready['total_bytes'],)*2)
        check('incomplete_master_phase_remains_non_executable',(await service.status(ready['plan_id']))['state']=='admitting')
        private=root/'private-resume.json';private.write_text(json.dumps(dict(url=str(f.master.database.url),
            vault_key=base64.b64encode(f.master.vault_key).decode(),task_id=ready['plan_id'],review_sha256=ready['review_sha256'])))
        private.chmod(0o600)
        process=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.resource_admission_native','--child',str(private),
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(process.communicate(),45)
        if process.returncode:raise AssertionError(err.decode(errors='replace')[-3000:])
        recovery=json.loads(out);check('actual_child_process_resumes_original_task_without_new_preview',recovery['child_process_recovery'])
        check('restart_does_not_double_reserve',await fixture.quota()==(ready['total_bytes'],)*2)
        status=await service.status(ready['plan_id'])
        check('master_and_two_native_preparations_complete',status['state']=='prepared' and status['peers']==dict(source='prepared',target='prepared'))
        check('no_staging_or_execution_proof_fabricated',not status['writable_verified'] and not status['execution_authorized'])
        try:await Journal(f.master.store).claim(ready['plan_id'],uuid.uuid4().hex)
        except ValueError:check('ordinary_transfer_runner_cannot_claim_preparation',True)
        else:raise AssertionError('prepared task became executable')
        result=await service.cancel_unstarted(ready['plan_id'],'native')
        check('controlled_abort_acknowledged_by_both_nodes',result['peers']==dict(source='aborted',target='aborted'))
        check('all_owned_master_and_target_quota_settled',await fixture.quota()==(0,0))
        check('source_originals_and_ciphertext_unchanged',all(path.read_bytes()==data for _,path,data in originals))
        check('no_target_media_created',not list(f.target.roots['media'].rglob('*.mp3')))
        check('control_wire_smaller_than_source_media',wire['sent_metadata_bytes']<sum(len(value[2]) for value in originals))
        samples=[]
        for _ in range(5):
            wall,cpu=time.monotonic(),time.process_time();await asyncio.sleep(1)
            samples.append(round(100*(time.process_time()-cpu)/(time.monotonic()-wall),3))
        check('five_quiescent_one_core_samples_under_twenty_percent',all(value<20 for value in samples))
        quota,period=(Path('/sys/fs/cgroup/cpu.max').read_text().strip().split())
        memory=int(Path('/sys/fs/cgroup/memory.max').read_text())
        check('actual_one_cpu_and_512mib_cgroup',quota==period and memory==512*1024**2)
        print(json.dumps(dict(format='frontiercloud-python-native-master-admission',version=1,count=len(checks),checks=checks,
            tls_verified=True,child_process_recovery=True,execution_authorized=False,media_decrypted=False,
            source_bytes=sum(len(value[2]) for value in originals),wire=wire,cpu_samples=samples,cpu_quota=int(quota),cpu_period=int(period),
            memory_limit=memory,peak_parent_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:
        await transport.close()
        for server in servers:server.should_exit=True
        await asyncio.wait_for(asyncio.gather(*tasks),5)
        for item in reversed(patches):item.stop()
        await fixture.asyncTearDown()


if __name__=='__main__':asyncio.run(child(sys.argv[2]) if len(sys.argv)>1 and sys.argv[1]=='--child' else main())
