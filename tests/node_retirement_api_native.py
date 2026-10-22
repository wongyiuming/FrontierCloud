"""Actual verified TLS, operation HMAC, lost reply and runtime-restart evidence."""
import asyncio
import copy
import json
import socket
import ssl
import time
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException,Request
import httpx
from sqlalchemy import update
import uvicorn

from app.api import internal_nodes,internal_retirement as api
from app.services import node_retirement_protocol as protocol,resource_fences,runtime_gate
from app.services.federation import protocol as p,schema as fs
from app.services.federation.state import State
from app.services.node_retirement_client import Coordinator
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_gate import GateError
from app.services.node_retirement_runtime import Runtime
from tests.certificate_fixtures import TestCA
from tests import test_node_retirement_api as fixtures


class Native:
    def __init__(self):self.fixture=None;self.listener=None;self.server=None;self.serving=None;self.inputs=[]

    def socket(self,port=0):
        listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',port));listener.listen(8);listener.setblocking(False);return listener

    async def open(self):
        self.fixture=fixtures.RetirementAPITests('runTest');await self.fixture.asyncSetUp();self.f=self.fixture.f
        await self.fixture.transport.close();self.listener=self.socket();self.port=self.listener.getsockname()[1]
        self.origin=f'https://node.4399.com:{self.port}'
        async with self.f.peer.database.begin() as conn:await conn.execute(update(fs.identity).values(endpoint=self.origin))
        self.f.peer.node['endpoint']=self.origin
        async with self.f.master.database.begin() as conn:await conn.execute(update(fs.relationships).values(peer_endpoint=self.origin))
        self.f.file.authority.close();self.f.file.authority=await self.f.file.capture();self.f.file.runtime.authority=self.f.file.authority;self.f.bind_peer()
        self.root=self.f.file.base;ca=TestCA();certificate,key=ca.pair()
        (self.root/'chain.pem').write_bytes(certificate);(self.root/'key.pem').write_bytes(key);(self.root/'key.pem').chmod(0o600)
        (self.root/'ca.pem').write_bytes(ca.root.public_bytes(serialization.Encoding.PEM))
        other=TestCA();(self.root/'other-ca.pem').write_bytes(other.root.public_bytes(serialization.Encoding.PEM))
        self.context=ssl.create_default_context(cafile=str(self.root/'ca.pem'))
        await self.start_server();await self.clients();await self.fixture.prepare()

    async def clients(self):
        async def record(request):
            self.inputs.append(dict(method=request.method,url=str(request.url),path=request.url.raw_path.decode(),
                headers={name:request.headers.get(name,'') for name in ('x-node-relationship','x-node-time','x-node-nonce','x-node-signature')},
                value=json.loads(request.content or b'{}')))
        self.fixture.transport.client=httpx.AsyncClient(verify=self.context,trust_env=False,follow_redirects=False,timeout=10,
            limits=httpx.Limits(max_connections=4,max_keepalive_connections=2),event_hooks={'request':[record]})
        self.fixture.transport.backup_client=httpx.AsyncClient(verify=self.context,trust_env=False,follow_redirects=False,timeout=10,
            limits=httpx.Limits(max_connections=1,max_keepalive_connections=1),event_hooks={'request':[record]})
        self.fixture.http=self.fixture.transport.client

    async def start_server(self):
        self.server=uvicorn.Server(uvicorn.Config(self.fixture.app,ssl_certfile=str(self.root/'chain.pem'),ssl_keyfile=str(self.root/'key.pem'),
            lifespan='off',access_log=False,log_level='error',workers=1))
        self.serving=asyncio.create_task(self.server.serve(sockets=[self.listener]))
        async with asyncio.timeout(5):
            while not self.server.started:
                if self.serving.done():await self.serving;raise RuntimeError('Native TLS server stopped')
                await asyncio.sleep(.01)

    async def stop_server(self):
        if self.server is not None:self.server.should_exit=True
        if self.serving is not None:await asyncio.wait_for(self.serving,5);self.serving=None
        if self.listener is not None:self.listener.close();self.listener=None

    async def negatives(self):
        value=self.fixture.value('status');raw=p.canonical(value);headers=self.fixture.headers(raw);before=await self.fixture.nonce_count();count=0
        for origin,cafile in ((self.origin,self.root/'other-ca.pem'),(self.origin.replace('node.4399.com','wrong.4399.com'),self.root/'ca.pem')):
            async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(cafile)),trust_env=False,timeout=2) as client:
                try:await client.post(origin+protocol.PATH,content=raw,headers=headers)
                except httpx.ConnectError as exc:
                    assert 'CERTIFICATE_VERIFY_FAILED' in str(exc),str(exc)
                    count+=1
                else:raise AssertionError('Untrusted TLS was accepted')
        assert count==2 and before==await self.fixture.nonce_count();return count

    async def replay(self,record):
        assert abs(int(time.time())-int(record['headers']['x-node-time']))<=p.AUTH_SKEW_SECONDS
        response=await self.fixture.http.post(self.origin+record['path'],content=p.canonical(record['value']),headers=record['headers'])
        assert response.status_code==401,response.text

    async def samples(self):
        result=[]
        for _ in range(5):
            wall,cpu=time.monotonic(),time.process_time();await asyncio.sleep(1)
            value=round(100*(time.process_time()-cpu)/(time.monotonic()-wall),3);assert value<20;result.append(value)
        return result

    async def close(self):
        if self.fixture is not None:await self.fixture.transport.close()
        await self.stop_server()
        if self.fixture is not None:await self.fixture.asyncTearDown();self.fixture=None


async def active_request():
    native=Native();release=asyncio.Event();authorized=asyncio.Event();held=None
    try:
        await native.open();fixture=native.fixture;f=native.f;negatives=await native.negatives()
        @fixture.app.post('/internal/v1/held')
        async def blocked(request:Request):
            await internal_nodes.authenticated(request);authorized.set();await release.wait()
            try:
                async with f.peer.database.begin() as conn:await resource_fences.new_upload(conn,f.peer.node['node_id'],'media','music/late/new.mp3')
            except RetirementError:raise HTTPException(409,'admission fenced') from None
            raise AssertionError('An already authenticated request bypassed retirement')
        raw=b'{}';path='/internal/v1/held'
        held=asyncio.create_task(fixture.http.post(native.origin+path,content=raw,headers=fixture.headers(raw,path=path)))
        await asyncio.wait_for(authorized.wait(),3)
        await fixture.client.call('begin',fixture.proposal['binding'])
        try:await fixture.client.call('complete',fixture.proposal['binding'])
        except RetirementError as exc:assert exc.code=='retirement_requests_busy'
        else:raise AssertionError('Active request did not block terminal completion')
        release.set();assert (await held).status_code==409
        peer=await fixture.client.call('complete',fixture.proposal['binding']);assert peer['phase']=='completed'
        status=await fixture.client.call('status',fixture.proposal['binding']);assert status['receipt']==peer['receipt']
        await native.replay(copy.deepcopy(native.inputs[-1]))
        raw=b'{}';path='/internal/v1/heartbeat'
        assert (await fixture.http.post(native.origin+path,content=raw,headers=fixture.headers(raw,path=path))).status_code==401
        master=await Coordinator(f.master_journal,fixture.transport).advance(f.relationship,f.peer.node['node_id'],'native-admin')
        assert master['phase']=='completed';await f.phase(f.peer,'completed');await f.phase(f.master,'completed')
        return dict(case='active_request',actual_https=True,actual_hmac=True,ordinary_old_credentials_refused=True,nonce_replay_refused=True,
            active_request_blocks_completion=True,late_admission_refused=True,peer_master_completed=True,tls_negative_cases=negatives,
            inputs=native.inputs,cpu_percent_samples=await native.samples())
    finally:
        release.set()
        if held is not None and not held.done():await asyncio.gather(held,return_exceptions=True)
        await native.close()


async def lost_reply_restart():
    native=Native();release=asyncio.Event();committed=asyncio.Event();pending=None
    try:
        await native.open();fixture=native.fixture;f=native.f;negatives=await native.negatives()
        await fixture.client.call('begin',fixture.proposal['binding']);original=api.LeasedResponse.__call__
        async def delayed(response,scope,receive,send):
            action=json.loads(response.body).get('payload',{}).get('action')
            async def delivered(message):
                if action=='complete' and message['type']=='http.response.body':committed.set();await release.wait()
                await send(message)
            await original(response,scope,receive,delivered)
        with patch.object(api.LeasedResponse,'__call__',delayed):
            pending=asyncio.create_task(fixture.client.call('complete',fixture.proposal['binding']))
            await asyncio.wait_for(committed.wait(),4)
            try:await asyncio.wait_for(pending,.2)
            except TimeoutError:pass
            else:raise AssertionError('Controlled lost response did not time out')
            assert (await fixture.http.get(native.origin+'/health/live')).status_code==200
            assert (await fixture.http.get(native.origin+'/internal/v1/identity?challenge='+'1'*32)).status_code==503
            release.set()
            async with asyncio.timeout(3):
                while True:
                    try:
                        with f.file.runtime.gate.lease():break
                    except GateError:await asyncio.sleep(.02)
        peer=await fixture.client.call('status',fixture.proposal['binding']);assert peer['phase']=='completed'
        old_request=copy.deepcopy(native.inputs[-1]);identity=dict(f.peer.node);vault=f.file.vault.read_bytes()
        await fixture.transport.close();await native.stop_server();f.file.runtime.close();f.file.native.close()
        f.file.native=runtime_gate.acquire(f.file.data);f.file.runtime=Runtime(f.file.data,f.file.native)
        f.peer=State(f.peer.database,vault);await f.peer.initialize();assert f.peer.node==identity;f.file.store=f.peer
        f.file.authority=await f.file.capture();f.file.runtime.authority=f.file.authority;f.bind_peer()
        api.state=f.peer;internal_nodes.state=f.peer
        native.listener=native.socket(native.port);await native.start_server();await native.clients()
        await native.replay(old_request)
        fresh=await fixture.client.call('status',fixture.proposal['binding']);assert fresh['receipt']==peer['receipt']
        replay=await fixture.client.call('complete',fixture.proposal['binding']);assert replay['receipt']==peer['receipt']
        master=await Coordinator(f.master_journal,fixture.transport).advance(f.relationship,f.peer.node['node_id'],'native-retry')
        assert master['receipt']['payload']['peer_receipt']==peer['receipt']
        await f.phase(f.peer,'completed');await f.phase(f.master,'completed');assert f.file.vault.read_bytes()==vault
        return dict(case='lost_reply_restart',actual_https=True,actual_hmac=True,lost_reply='caller_timeout_after_commit',runtime_and_store_restarted=True,
            durable_nonce_replay_refused=True,identical_peer_receipt=True,identity_and_vault_retained=True,peer_master_completed=True,
            health_under_exclusive=True,new_internal_under_exclusive_refused=True,tls_negative_cases=negatives,
            inputs=native.inputs,cpu_percent_samples=await native.samples())
    finally:
        release.set()
        if pending is not None and not pending.done():pending.cancel();await asyncio.gather(pending,return_exceptions=True)
        await native.close()


async def run():
    started=time.monotonic();cases=[await active_request(),await lost_reply_restart()]
    print(json.dumps(dict(format='frontiercloud-python-retirement-api-https',version=1,cases=cases,
        actual_retirement_http=True,admin_gui=False,full_retirement_acceptance=False,production_updated=False,
        elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True),flush=True)


if __name__=='__main__':asyncio.run(run())
