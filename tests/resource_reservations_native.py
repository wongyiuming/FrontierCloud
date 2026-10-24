"""Verified HTTPS native preparation, replay refusal and child-process SQL reload.

Two real ASGI endpoints run inside one CPU-limited fixture. This is preparation
evidence, not separate-host transfer, publication or a full migration claim.
"""
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

from app.api import internal_nodes, internal_resource_reservations as api, internal_resource_inventory as control_api
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_crypto as crypto, resource_fences
from app.services.federation import protocol as p, schema as fs
from app.services.federation.state import State
from app.services.federation.transport import Transport
from app.services.resource_peer_reservations import NativeReservations
from app.services.resource_reservation_client import ReservationClient
from tests.certificate_fixtures import TestCA
from tests import test_resource_peer_reservations as fixtures


async def child(path):
    private=json.loads(Path(path).read_text())
    result=[]
    for entry in private:
        database=create_sqlite_engine(make_url(entry['url']))
        try:
            store=State(database,base64.b64decode(entry['vault_key']));await store.initialize()
            async with database.connect() as conn:
                relation=dict((await conn.execute(select(fs.relationships).where(
                    fs.relationships.c.relationship_id==entry['relationship']))).mappings().one())
            status=await NativeReservations(store).status(store.node,relation,uuid.uuid4().hex,entry['task_id'])
            assert status['state']=='prepared' and status['capacity_reserved']==entry['reserved']
            async with database.begin() as conn:
                try:await resource_fences.node_clear(conn)
                except resource_fences.FenceConflict:pass
                else:raise AssertionError('child_reload_lost_native_authority')
            result.append(dict(side=status['side'],state=status['state'],capacity_reserved=status['capacity_reserved']))
        finally:await database.dispose()
    print(json.dumps(dict(child_process_reload=True,nodes=result)))


async def main():
    started,cpu=time.monotonic(),time.process_time()
    fixture=fixtures.PeerReservationTests('runTest');await fixture.asyncSetUp()
    f=fixture.f;selected=contextvars.ContextVar('native_reservation_peer')
    checks=[];wire=dict(requests=0,received_metadata_bytes=0,sent_metadata_bytes=0)
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
    root=f.root
    ca,wrong=TestCA(),TestCA();chain,key=ca.pair()
    for name,value in (('chain.pem',chain),('key.pem',key),('ca.pem',ca.root.public_bytes(serialization.Encoding.PEM)),
                       ('wrong.pem',wrong.root.public_bytes(serialization.Encoding.PEM))):
        (root/name).write_bytes(value)
    (root/'key.pem').chmod(0o600)
    payloads=[await f.media(b'ID3'+bytes(2*1024*1024)),await f.media(b'private'*4000,path='music/album/private.mp3',encrypted=True)]
    fixture.units=[fixture.unit('music/album',count=len(payloads),size=sum(len(data) for _,_,data in payloads))]
    fixture.request=fixture.reservation()
    servers=[];tasks=[];transport=Transport()
    patches=[patch.object(internal_nodes,'state',Proxy('store')),patch.object(api,'state',Proxy('store')),
        patch.object(control_api,'state',Proxy('store')),
        patch.object(api,'reservations',Proxy('reservations')),patch.object(internal_nodes.settings,'TLS_ENABLED',True),
        patch.dict(os.environ,{'SSL_CERT_FILE':str(root/'ca.pem')})]
    try:
        for item in patches:item.start()
        peers=(f.source,f.target)
        for port,peer in enumerate(peers,18763):
            origin=f'https://node.4399.com:{port}'
            peer.store.node['endpoint']=origin
            async with peer.database.begin() as conn:await conn.execute(update(fs.identity).values(endpoint=origin))
            async with f.master.database.begin() as conn:
                await conn.execute(update(fs.relationships).where(fs.relationships.c.peer_id==peer.store.node['node_id']).values(peer_endpoint=origin))
            app=FastAPI();app.include_router(internal_nodes.router);app.include_router(api.router)
            server=uvicorn.Server(uvicorn.Config(Bound(app,peer),host='127.0.0.1',port=port,ssl_certfile=str(root/'chain.pem'),
                ssl_keyfile=str(root/'key.pem'),lifespan='off',access_log=False,log_level='error',workers=1))
            servers.append(server);tasks.append(asyncio.create_task(server.serve()))
        async with asyncio.timeout(5):
            while not all(server.started for server in servers):
                for task in tasks:
                    if task.done():await task;raise AssertionError('native_tls_server_stopped')
                await asyncio.sleep(.01)
        for name,origin,context in (
            ('wrong_ca_rejected',f.source.store.node['endpoint'],ssl.create_default_context(cafile=str(root/'wrong.pem'))),
            ('wrong_san_rejected',f.source.store.node['endpoint'].replace('node.4399.com','wrong.4399.com'),ssl.create_default_context(cafile=str(root/'ca.pem')))):
            async with httpx.AsyncClient(verify=context,trust_env=False,timeout=3) as client:
                try:await client.get(origin+'/internal/v1/identity',params=dict(challenge=uuid.uuid4().hex))
                except httpx.ConnectError:check(name,True)
                else:raise AssertionError(name+'_accepted')
        clients=[];private=[]
        with (patch.object(crypto,'open_premaster',side_effect=AssertionError('premaster accessed')),
              patch.object(crypto.Manager,'file_key',side_effect=AssertionError('media decrypted'))):
            for peer,side in zip(peers,('source','target')):
                request=fixture.reservation(side=side,task=fixture.request['task_id'])
                async with f.master.database.connect() as conn:
                    relation=dict((await conn.execute(select(fs.relationships).where(
                        fs.relationships.c.peer_id==peer.store.node['node_id']))).mappings().one())
                await transport.identity(peer.store.node['endpoint'],expected_id=peer.store.node['node_id'],expected_key=relation['peer_key'],role='Follower')
                client=ReservationClient(f.master.store,relation,transport,request);clients.append(client)
                first,again=await client.begin(),await client.begin()
                check(side+'_native_idempotent_reservation',first['revision']==again['revision'] and
                    again['capacity_reserved']==(request['total_bytes'] if side=='target' else 0))
                await client.add_unit(fixture.units[0]);prepared=await client.seal()
                check(side+'_whole_parent_prepared_without_execution',prepared['state']=='prepared' and not prepared['execution_authorized'])
                private.append(dict(url=str(peer.database.url),vault_key=base64.b64encode(peer.vault_key).decode(),
                    relationship=peer.relationship,task_id=request['task_id'],reserved=prepared['capacity_reserved']))
            context=ssl.create_default_context(cafile=str(root/'ca.pem'))
            async with httpx.AsyncClient(verify=context,trust_env=False,timeout=3) as client:
                path='/internal/v1/migration/reservations/status'
                value=dict(request_id=uuid.uuid4().hex,task_id=clients[0].reservation['task_id']);body=p.canonical(value)
                headers=p.auth_headers(f.source.credential,f.source.relationship,'POST',path,body)
                first=await client.post(f.source.store.node['endpoint']+path,content=body,headers=headers)
                replay=await client.post(f.source.store.node['endpoint']+path,content=body,headers=headers)
                check('actual_tls_nonce_replay_refused',first.status_code==200 and replay.status_code==401)
            path=root/'private-reload.json';path.write_text(json.dumps(private));path.chmod(0o600)
            process=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.resource_reservations_native','--child',str(path),
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,stderr=await asyncio.wait_for(process.communicate(),15)
            check('separate_process_sql_reload_preserves_reservations',process.returncode==0 and json.loads(stdout)['child_process_reload'])
            for peer in peers:
                try:
                    async with peer.database.begin() as conn:await resource_fences.node_clear(conn)
                except resource_fences.FenceConflict:pass
                else:raise AssertionError('prepared_node_reset_allowed')
            check('prepared_native_identity_cannot_reset',True)
            for client in clients:
                result=await client.abort_unstarted()
                check(client.reservation['side']+'_controlled_unstarted_abort',result['state']=='aborted' and result['capacity_reserved']==0)
            for peer in peers:
                async with peer.database.begin() as conn:
                    await resource_fences.node_clear(conn)
                    check(peer.name+'_capacity_and_fences_settled',await conn.scalar(select(fs.storage_members.c.reserved_bytes))==0)
            check('source_plain_and_ciphertext_unchanged',all(path.read_bytes()==data for _,path,data in payloads))
            check('control_bytes_bounded_and_no_payload_relay',wire['received_metadata_bytes']<64*1024 and wire['sent_metadata_bytes']<64*1024)
        quota,period=map(int,Path('/sys/fs/cgroup/cpu.max').read_text().split())
        memory=int(Path('/sys/fs/cgroup/memory.max').read_text())
        check('actual_container_one_cpu_512m',quota==period and memory==512*1024**2)
        samples=[];low=0;deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            before_wall,before_cpu=time.monotonic(),time.process_time()
            await asyncio.sleep(1)
            value=round((time.process_time()-before_cpu)/(time.monotonic()-before_wall)*100,3)
            samples.append(value);low=low+1 if value<20 else 0
            if low>=5:break
        check('native_web_process_quiescence',low>=5)
        print(json.dumps(dict(format='frontiercloud-python-native-peer-preparation',version=1,checks=checks,count=len(checks),
            tls_verified=True,media_decrypted=False,execution_authorized=False,source_changed=False,wire=wire,
            cpu_samples=samples,cpu_quota=quota,cpu_period=period,memory_limit=memory,
            seconds=round(time.monotonic()-started,4),cpu_seconds=round(time.process_time()-cpu,4),
            peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),sort_keys=True))
    finally:
        await transport.close()
        for server in servers:server.should_exit=True
        if tasks:await asyncio.wait_for(asyncio.gather(*tasks),5)
        for item in reversed(patches):item.stop()
        await fixture.asyncTearDown()


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--child':asyncio.run(child(sys.argv[2]))
    else:asyncio.run(main())
