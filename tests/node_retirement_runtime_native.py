"""Verified HTTPS/HMAC race evidence for live request fencing and native census."""
import asyncio
import json
import os
from pathlib import Path
import socket
import ssl
import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import Response
import httpx
from sqlalchemy import insert,update
import uvicorn

from app.api import internal_nodes
from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.middleware.node_retirement import NodeRetirementMiddleware
from app.services import resource_fences
from app.services.federation import protocol as p,schema as fs
from app.services.federation.state import State
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_gate import GateError
from app.services.node_retirement_state import Journal
from tests.certificate_fixtures import TestCA
from tests.test_node_retirement_files import RetirementFilesTests


async def exercise(guarded):
    fixture=RetirementFilesTests('runTest');await fixture.asyncSetUp()
    master_database=None;serving=None;request_task=None;listener=None
    released=asyncio.Event();authorized=asyncio.Event();observations=[]
    try:
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(8);listener.setblocking(False)
        port=listener.getsockname()[1];origin=f'https://node.4399.com:{port}'
        store=fixture.store
        async with store.database.begin() as conn:await conn.execute(update(fs.identity).values(endpoint=origin))
        store.node['endpoint']=origin;fixture.authority.close();fixture.authority=await fixture.capture()
        master_database=create_sqlite_engine('sqlite+aiosqlite:///'+str(fixture.base/'master.db'))
        with patch.object(db,'engine',master_database),patch.object(db,'SECRET_DIR',fixture.base/'unused-master'):await db.init_db()
        master=State(master_database,Fernet.generate_key());await master.initialize()
        async with master_database.begin() as conn:await conn.execute(update(fs.identity).values(role='Master',endpoint='https://master.4399.com'))
        master.node.update(role='Master',endpoint='https://master.4399.com')
        relationship='b'*32;credential=p.encode(os.urandom(48))
        for local,remote,direction in ((master,store,'downstream'),(store,master,'upstream')):
            async with local.database.begin() as conn:
                await conn.execute(insert(fs.relationships).values(relationship_id=relationship,peer_id=remote.node['node_id'],
                    peer_endpoint=remote.node['endpoint'],peer_key=p.public_key(remote.unseal(remote.node['private_key'])),
                    credential=local.seal(credential),direction=direction,mode='Direct',state='active',status='online',last_heartbeat=int(time.time()),
                    rtt_ms=1,failures=0,recoveries=0,peer_version=p.APP_VERSION,protocol=p.PROTOCOL_VERSION,summary={},created_at=1))
        async with master.database.begin() as conn:
            await conn.execute(insert(fs.storage_members).values(member_id=store.node['node_id'],relationship_id=relationship,
                member_kind='Follower',transport='Direct',storage_enabled=1,allocated_bytes=1024**3,used_bytes=0,reserved_bytes=0,
                physical_free_bytes=1024**3,health='online',writable=1,updated_at=1))
        ca=TestCA();certificate,key=ca.pair();root=fixture.base
        (root/'chain.pem').write_bytes(certificate);(root/'key.pem').write_bytes(key);(root/'key.pem').chmod(0o600)
        (root/'ca.pem').write_bytes(ca.root.public_bytes(serialization.Encoding.PEM))
        other=TestCA();(root/'other-ca.pem').write_bytes(other.root.public_bytes(serialization.Encoding.PEM))
        application=FastAPI();target=fixture.paths['media']/'music/late/admitted.mp3'
        @application.post('/internal/v1/held')
        async def held(request:Request):
            try:await internal_nodes.authenticated(request)
            except p.ProtocolError:raise HTTPException(401,'relationship denied') from None
            authorized.set();await released.wait()
            if guarded:
                try:
                    async with store.database.begin() as conn:await resource_fences.new_upload(conn,store.node['node_id'],'media','music/late/admitted.mp3')
                except RetirementError:raise HTTPException(409,'retirement fenced admission') from None
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'unknown file from a deliberately unguarded test handler')
            return Response(status_code=204)
        @application.get('/health/live')
        async def live():return {'healthy':True}
        application.add_middleware(NodeRetirementMiddleware,runtime_provider=lambda:fixture.runtime)
        server=uvicorn.Server(uvicorn.Config(application,ssl_certfile=str(root/'chain.pem'),ssl_keyfile=str(root/'key.pem'),
            lifespan='off',access_log=False,log_level='error',workers=1))
        with patch.object(internal_nodes,'state',store),patch.object(internal_nodes.settings,'TLS_ENABLED',True):
            serving=asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if serving.done():await serving;raise RuntimeError('Native TLS server stopped')
                        await asyncio.sleep(.01)
                for endpoint,ca_path in ((origin,root/'other-ca.pem'),(origin.replace('node.4399.com','wrong.4399.com'),root/'ca.pem')):
                    async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(ca_path)),trust_env=False,timeout=3) as rejected:
                        try:await rejected.get(endpoint+'/health/live')
                        except httpx.ConnectError:observations.append('invalid TLS trust/hostname refused')
                        else:raise AssertionError('Untrusted HTTPS was accepted')
                async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(root/'ca.pem')),trust_env=False,timeout=15) as client:
                    raw=b'{}';path='/internal/v1/held'
                    request_task=asyncio.create_task(client.post(origin+path,content=raw,headers=p.auth_headers(credential,relationship,'POST',path,raw)))
                    await asyncio.wait_for(authorized.wait(),5)
                    proposal=await Journal(master).begin_master(relationship,store.node['node_id'],'native-test-admin')
                    peer_journal=Journal(store,gate=fixture.runtime.gate,authority=fixture.authority)
                    prepared=await peer_journal.begin_peer(relationship,proposal['binding'])
                    assert prepared['roots']['native']==fixture.authority.binding()
                    try:fixture.runtime.gate.lease(exclusive=True)
                    except GateError as error:assert error.code=='retirement_requests_busy'
                    else:raise AssertionError('An authenticated live handler was not joined')
                    released.set();response=await request_task
                    assert response.status_code==(409 if guarded else 204),response.text
                    assert target.exists() is not guarded
                    response=await client.post(origin+path,content=raw,headers=p.auth_headers(credential,relationship,'POST',path,raw))
                    assert response.status_code==401,response.text
                    with fixture.runtime.gate.lease(exclusive=True):
                        assert (await client.get(origin+'/health/live')).status_code==200
                        assert (await client.post(origin+path,content=raw,headers=p.auth_headers(credential,relationship,'POST',path,raw))).status_code==503
                    if guarded:
                        inspected,_=await fixture.inspect();assert inspected['retained_files']==5
                    else:
                        try:await fixture.inspect()
                        except RetirementError as error:assert error.code=='retirement_unknown_file'
                        else:raise AssertionError('Late unknown data was admitted as clear')
                        assert target.read_bytes()==b'unknown file from a deliberately unguarded test handler'
                    samples=[]
                    for _ in range(5):
                        wall,cpu=time.monotonic(),time.process_time();await asyncio.sleep(1)
                        percent=100*(time.process_time()-cpu)/(time.monotonic()-wall);samples.append(round(percent,3));assert percent<20
                    return dict(guarded_admission=guarded,actual_https=True,actual_hmac=True,authenticated_before_fence=True,
                        active_request_blocks_exclusive=True,old_credentials_refused=True,health_available_under_exclusive=True,
                        new_internal_request_refused_under_exclusive=True,late_admission_status=409 if guarded else 204,
                        unknown_payload_retained=not guarded,unknown_payload_blocks_clearance=not guarded,
                        tls_negative_cases=len(observations),cpu_percent_samples=samples,terminal_retirement=False)
            finally:
                released.set()
                if request_task is not None and not request_task.done():request_task.cancel();await asyncio.gather(request_task,return_exceptions=True)
                server.should_exit=True;await asyncio.wait_for(serving,5)
    finally:
        if listener is not None:listener.close()
        if master_database is not None:await master_database.dispose()
        await fixture.asyncTearDown()


async def run():
    started=time.monotonic();cases=[await exercise(True),await exercise(False)]
    print(json.dumps(dict(format='frontiercloud-python-retirement-runtime-https',version=1,cases=cases,
        elapsed_seconds=round(time.monotonic()-started,4),terminal_retirement=False,production_updated=False),sort_keys=True),flush=True)


if __name__=='__main__':asyncio.run(run())
