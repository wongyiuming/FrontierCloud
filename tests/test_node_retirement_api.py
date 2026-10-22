"""Actual SQL/native core behind ASGI/HMAC controls; TLS is a separate gate."""
import asyncio
import copy
import os
import sqlite3
import time
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI,Request
import httpx
from sqlalchemy import func,insert,select,update

from app.api import internal_nodes,internal_retirement as api
from app.core.config import settings
from app.middleware.node_retirement import NodeRetirementMiddleware
from app.services import business_traffic,node_retirement_protocol as protocol,node_retirement_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.federation.transport import Transport
from app.services.node_retirement_auth import Authentication,AuthenticationError
from app.services.node_retirement_client import Client,Coordinator
from app.services.node_retirement_complete import Completion
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_gate import GateError
from app.services.node_retirement_state import Journal
from tests import test_node_retirement_complete as fixtures,test_node_retirement_runtime as requests


@unittest.skipUnless(os.name=='posix','retirement controls require pinned Linux native files')
class RetirementAPIFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=fixtures.RetirementCompletionTests('runTest');await self.f.asyncSetUp()
        self.f.master_journal=Journal(self.f.master);self.f.master_complete=Completion(self.f.master_journal)
        self.patches=[patch.object(api,'state',self.f.peer),patch.object(api,'current',lambda:self.f.file.runtime),
            patch.object(internal_nodes,'state',self.f.peer),patch.object(settings,'TLS_ENABLED',True),
            patch.object(business_traffic,'monitor',self.f.traffic)]
        for item in self.patches:item.start()
        self.app=FastAPI();self.app.include_router(api.router);self.app.include_router(internal_nodes.router)
        @self.app.get('/health/live')
        async def health():return {'healthy':True}
        self.app.add_middleware(NodeRetirementMiddleware,runtime_provider=lambda:self.f.file.runtime)
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url=self.f.peer.node['endpoint'])
        self.transport=Transport();self.transport.client=self.http;self.transport.backup_client=self.http

    async def asyncTearDown(self):
        await self.transport.close()
        for item in reversed(self.patches):item.stop()
        await self.f.asyncTearDown()

    async def prepare(self):
        self.proposal=await self.f.master_journal.begin_master(self.f.relationship,self.f.peer.node['node_id'],'test-admin')
        self.relation=await self.f.master.relationship(self.f.relationship)
        self.client=Client(self.f.master,self.relation,self.transport)
        return self.proposal

    def value(self,action,*,binding=None):
        return dict(format=protocol.FORMAT,version=1,action=action,request_id=uuid.uuid4().hex,
            binding=self.proposal['binding'] if binding is None else binding)

    def headers(self,raw,*,path=protocol.PATH,credential=None,nonce=None,stamp=None):
        with patch.object(p.secrets,'token_hex',return_value=nonce or uuid.uuid4().hex),patch.object(p.time,'time',return_value=time.time() if stamp is None else stamp):
            return p.auth_headers(credential or self.f.credential,self.f.relationship,'POST',path,raw)

    async def send(self,value,*,raw=None,headers=None,path=protocol.PATH):
        raw=p.canonical(value) if raw is None else raw
        return await self.http.post(path,content=raw,headers=headers or self.headers(raw,path=path))

    async def nonce_count(self):
        async with self.f.peer.database.connect() as conn:return await conn.scalar(select(func.count()).select_from(fs.requests))


class RetirementAPITests(RetirementAPIFixture):
    async def test_master_coordinator_completes_the_actual_peer_api_and_replays_locally(self):
        await self.prepare();coordinator=Coordinator(self.f.master_journal,self.transport)
        first=await coordinator.advance(self.f.relationship,self.f.peer.node['node_id'],'test-admin')
        self.assertEqual(first['phase'],'completed')
        await self.f.phase(self.f.peer,'completed');await self.f.phase(self.f.master,'completed')
        with patch.object(self.transport,'request',side_effect=AssertionError('recontacted a completed peer')):
            self.assertEqual(await coordinator.advance(self.f.relationship,self.f.peer.node['node_id'],'retry'),first)
        path='/internal/v1/heartbeat';raw=b'{}'
        response=await self.http.post(path,content=raw,headers=self.headers(raw,path=path));self.assertEqual(response.status_code,401)

    async def test_completed_status_and_complete_return_the_same_receipt_with_new_nonces(self):
        await self.prepare();await self.client.call('begin',self.proposal['binding']);first=await self.client.call('complete',self.proposal['binding'])
        for action in ('status','complete','status'):
            result=await self.client.call(action,self.proposal['binding'])
            self.assertEqual(result['receipt'],first['receipt']);self.assertEqual(result['completed_at'],first['completed_at'])
        self.assertEqual((await self.send(self.value('begin'))).status_code,401)
        await self.f.phase(self.f.peer,'completed')

    async def test_same_headers_and_resigned_same_nonce_are_rejected(self):
        await self.prepare();value=self.value('begin');raw=p.canonical(value);nonce=uuid.uuid4().hex;headers=self.headers(raw,nonce=nonce)
        self.assertEqual((await self.send(value,headers=headers)).status_code,200)
        self.assertEqual((await self.send(value,headers=headers)).status_code,401)
        changed=self.value('status');raw=p.canonical(changed)
        self.assertEqual((await self.send(changed,headers=self.headers(raw,nonce=nonce))).status_code,401)
        self.assertEqual(await self.nonce_count(),1)

    async def test_ordinary_and_retirement_controls_share_the_durable_nonce_scope(self):
        await self.prepare();path='/internal/v1/heartbeat';raw=b'{}';nonce=uuid.uuid4().hex
        self.assertEqual((await self.http.post(path,content=raw,headers=self.headers(raw,path=path,nonce=nonce))).status_code,200)
        value=self.value('status');raw=p.canonical(value)
        self.assertEqual((await self.send(value,headers=self.headers(raw,nonce=nonce))).status_code,401)
        self.assertEqual(await self.nonce_count(),1)

    async def test_wrong_node_operation_endpoint_or_transport_binding_has_no_authority(self):
        await self.prepare()
        for name,value in (('member_id','f'*32),('master_endpoint','https://other.4399.com'),('mode','Relay')):
            altered={**self.proposal['binding'],name:value}
            self.assertEqual((await self.send(self.value('begin',binding=altered))).status_code,401)
        self.assertEqual(await self.nonce_count(),0)
        # An authenticated Master chooses the ID for the first preparation.
        # Once stored, even that Master cannot replace it with another ID.
        await self.client.call('begin',self.proposal['binding'])
        altered={**self.proposal['binding'],'retirement_id':'f'*32}
        self.assertEqual((await self.send(self.value('begin',binding=altered))).status_code,401)
        self.assertEqual(await self.nonce_count(),1)
        async with self.f.peer.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.retirements.c.retirement_id)),self.proposal['retirement_id'])

    async def test_bad_credential_old_timestamp_query_or_body_mac_is_refused(self):
        await self.prepare();value=self.value('begin');raw=p.canonical(value)
        variants=[self.headers(raw,credential=p.encode(os.urandom(48))),self.headers(raw,stamp=time.time()-61),self.headers(b'{}')]
        for headers in variants:self.assertEqual((await self.send(value,headers=headers)).status_code,401)
        self.assertEqual((await self.send(value,path=protocol.PATH+'?action=status')).status_code,401)
        self.assertEqual(await self.nonce_count(),0)

    async def test_duplicate_headers_and_plain_http_do_not_consume_a_nonce(self):
        await self.prepare();value=self.value('begin');raw=p.canonical(value);headers=list(self.headers(raw).items())+[('X-Node-Nonce','f'*32)]
        self.assertEqual((await self.send(value,headers=headers)).status_code,401)
        self.assertEqual((await self.http.post('http://native.4399.com'+protocol.PATH,content=raw,headers=self.headers(raw))).status_code,403)
        self.assertEqual(await self.nonce_count(),0)

    async def test_duplicate_json_nonfinite_and_oversized_bodies_never_reach_authorization(self):
        await self.prepare();value=self.value('begin');raw=p.canonical(value)
        bodies=[raw[:-1]+b',"action":"complete"}',b'[]',b'{"number":NaN}',p.canonical({**value,'extra':True})]
        for invalid in bodies:self.assertEqual((await self.send(value,raw=invalid)).status_code,400)
        self.assertEqual((await self.send(value,raw=b'x'*(protocol.LIMIT+1))).status_code,413)
        self.assertEqual(await self.nonce_count(),0)

    async def test_binding_boolean_or_float_versions_cannot_be_persisted(self):
        await self.prepare()
        for name,value in (('version',True),('version',1.0),('protocol',2.0),('protocol',True)):
            self.assertEqual((await self.send(self.value('begin',binding={**self.proposal['binding'],name:value}))).status_code,400)
        self.assertEqual(await self.nonce_count(),0)
        async with self.f.peer.database.connect() as conn:self.assertIsNone(await conn.scalar(select(s.retirements.c.retirement_id)))

    async def test_missing_startup_authority_cannot_create_a_gate_only_preparation(self):
        await self.prepare();authority=self.f.file.runtime.authority;self.f.file.runtime.authority=None
        try:
            with self.assertRaisesRegex(RetirementError,'retirement_native_observation_unavailable'):
                await self.client.call('begin',self.proposal['binding'])
        finally:self.f.file.runtime.authority=authority
        async with self.f.peer.database.connect() as conn:self.assertIsNone(await conn.scalar(select(s.retirements.c.retirement_id)))

    async def test_precise_signed_unknown_file_error_preserves_material_and_fence(self):
        await self.prepare();await self.client.call('begin',self.proposal['binding'])
        path=self.f.file.paths['media']/'unmanaged';path.write_bytes(b'unchanged unknown bytes')
        with self.assertRaisesRegex(RetirementError,'retirement_unknown_file'):await self.client.call('complete',self.proposal['binding'])
        self.assertEqual(path.read_bytes(),b'unchanged unknown bytes');await self.f.phase(self.f.peer,'retiring')
        with self.f.file.runtime.gate.lease(exclusive=True):pass

    async def test_concurrent_requests_cannot_consume_the_same_nonce_twice(self):
        await self.prepare();value=self.value('status');raw=p.canonical(value);headers=self.headers(raw)
        responses=await asyncio.gather(self.send(value,headers=headers),self.send(value,headers=headers))
        self.assertEqual(sorted(response.status_code for response in responses),[401,409]);self.assertEqual(await self.nonce_count(),1)

    async def _held_response(self,action,*,cancel=False):
        value=self.value(action);raw=p.canonical(value);headers=[(key.lower().encode(),item.encode()) for key,item in self.headers(raw).items()]
        started=asyncio.Event();release=asyncio.Event();messages=[]
        async def receive():return {'type':'http.request','body':raw,'more_body':False}
        async def send(message):
            messages.append(message)
            if message['type']=='http.response.body':started.set();await release.wait()
        task=asyncio.create_task(self.app(requests.scope(protocol.PATH,'POST',headers),receive,send))
        try:
            await asyncio.wait_for(started.wait(),3);self.assertEqual(messages[0]['status'],200)
            with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.f.file.runtime.gate.lease(exclusive=True)
            self.assertEqual((await self.http.get('/health/live')).status_code,200)
            if action=='complete':self.assertEqual((await self.http.get('/internal/v1/identity?challenge='+'1'*32)).status_code,503)
            if cancel:task.cancel()
        finally:release.set()
        if cancel:
            with self.assertRaises(asyncio.CancelledError):await task
        else:await task
        with self.f.file.runtime.gate.lease(exclusive=True):pass

    async def test_begin_and_complete_keep_their_actual_lease_until_response_finishes(self):
        await self.prepare();await self._held_response('begin');await self._held_response('complete')
        await self.f.phase(self.f.peer,'completed')

    async def test_lost_terminal_response_releases_the_lease_and_replays_the_receipt(self):
        await self.prepare();await self.client.call('begin',self.proposal['binding']);await self._held_response('complete',cancel=True)
        first=await self.client.call('status',self.proposal['binding']);second=await self.client.call('complete',self.proposal['binding'])
        self.assertEqual(first['receipt'],second['receipt']);await self.f.phase(self.f.peer,'completed')

    async def test_active_shared_request_returns_signed_busy_then_completes_on_retry(self):
        await self.prepare();await self.client.call('begin',self.proposal['binding'])
        with self.f.file.runtime.gate.lease():
            with self.assertRaisesRegex(RetirementError,'retirement_requests_busy'):await self.client.call('complete',self.proposal['binding'])
        await self.f.phase(self.f.peer,'retiring');await self.client.call('complete',self.proposal['binding']);await self.f.phase(self.f.peer,'completed')

    async def test_client_rejects_a_valid_old_reply_for_a_new_request_id(self):
        await self.prepare();await self.client.call('begin',self.proposal['binding']);original=self.transport.request;saved=[]
        async def replay(*args,**kwargs):
            if not saved:saved.append(await original(*args,**kwargs))
            return saved[0]
        with patch.object(self.transport,'request',side_effect=replay):
            await self.client.call('status',self.proposal['binding'])
            with self.assertRaisesRegex(RetirementError,'retirement_signed_response_invalid'):await self.client.call('status',self.proposal['binding'])

    async def test_future_timestamp_replay_is_retained_through_the_inclusive_skew_boundary(self):
        await self.prepare();value=self.value('status');raw=p.canonical(value);now=int(time.time());headers=httpx.Headers(self.headers(raw,stamp=now+60))
        auth=Authentication(self.f.peer_journal,wall=lambda:now);await auth.check(headers,'POST',protocol.PATH,raw,value)
        now+=120
        with self.assertRaises(AuthenticationError):await auth.check(headers,'POST',protocol.PATH,raw,value)
        async with self.f.peer.database.connect() as conn:self.assertEqual(await conn.scalar(select(fs.requests.c.expires_at)),now+1)
        now+=1
        with self.assertRaises(AuthenticationError):await auth.check(headers,'POST',protocol.PATH,raw,value)

    async def test_nonce_reaping_is_bounded_and_does_not_touch_another_relationship(self):
        await self.prepare();now=int(time.time())
        async with self.f.peer.database.begin() as conn:
            for index in range(70):await conn.execute(insert(fs.requests).values(relationship_id=self.f.relationship,nonce=f'{index:032x}',expires_at=now-1))
            await conn.execute(insert(fs.requests).values(relationship_id='d'*32,nonce='1'*32,expires_at=now-1))
        value=self.value('status');raw=p.canonical(value)
        await Authentication(self.f.peer_journal,wall=lambda:now).check(httpx.Headers(self.headers(raw,stamp=now)),'POST',protocol.PATH,raw,value)
        async with self.f.peer.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(fs.requests).where(fs.requests.c.relationship_id==self.f.relationship)),39)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(fs.requests).where(fs.requests.c.relationship_id=='d'*32)),1)

    async def test_authenticator_refuses_other_paths_methods_and_a_mismatched_parsed_body(self):
        await self.prepare();value=self.value('status');raw=p.canonical(value);headers=httpx.Headers(self.headers(raw));auth=Authentication(self.f.peer_journal)
        for method,path,parsed in (('GET',protocol.PATH,value),('POST','/internal/v1/revoke',value),('POST',protocol.PATH,{**value,'action':'complete'})):
            with self.assertRaises(AuthenticationError):await auth.check(headers,method,path,raw,parsed)
        self.assertEqual(await self.nonce_count(),0)

    async def test_credential_replacement_after_authentication_cannot_prepare_retirement(self):
        await self.prepare();original=Authentication.check
        async def replaced(auth,*args):
            relation=await original(auth,*args)
            async with self.f.peer.database.begin() as conn:
                await conn.execute(update(fs.relationships).values(credential=self.f.peer.seal(p.encode(os.urandom(48)))))
            return relation
        with patch.object(Authentication,'check',replaced):
            with self.assertRaisesRegex(RetirementError,'retirement_authenticated_relation_changed'):
                await self.client.call('begin',self.proposal['binding'])
        async with self.f.peer.database.connect() as conn:self.assertIsNone(await conn.scalar(select(s.retirements.c.retirement_id)))

    async def test_status_does_not_return_an_operation_started_after_its_authentication(self):
        await self.prepare();original=Authentication.check
        other={**self.proposal['binding'],'retirement_id':'f'*32}
        async def started(auth,*args):
            relation=await original(auth,*args);await self.f.peer_journal.begin_peer(self.f.relationship,other);return relation
        with patch.object(Authentication,'check',started):
            with self.assertRaisesRegex(RetirementError,'retirement_operation_conflict'):
                await self.client.call('status',self.proposal['binding'])
        async with self.f.peer.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.retirements.c.retirement_id)),'f'*32)


@unittest.skipIf(os.environ.get('FC_DB_MYSQL_RUNTIME')=='1','SQLite-only selected-file substitution')
class RetirementSQLiteSelectionTests(RetirementAPIFixture):
    async def test_completed_receipt_refuses_a_different_selected_database_even_with_copied_rows(self):
        await self.prepare();await self.client.call('begin',self.proposal['binding']);await self.client.call('complete',self.proposal['binding'])
        original=self.f.peer.database;target=self.f.file.base/'copied-state.db'
        source=sqlite3.connect(original.url.database);destination=sqlite3.connect(target)
        try:source.backup(destination)
        finally:destination.close();source.close()
        target.chmod(0o600)
        replacement=fixtures.create_sqlite_engine('sqlite+aiosqlite:///'+str(target));self.f.peer.database=replacement
        try:
            with self.assertRaisesRegex(RetirementError,'retirement_selected_database_changed'):
                await self.client.call('status',self.proposal['binding'])
        finally:self.f.peer.database=original;await replacement.dispose()
        self.assertTrue(target.exists())
        self.assertEqual((await self.client.call('status',self.proposal['binding']))['phase'],'completed')
