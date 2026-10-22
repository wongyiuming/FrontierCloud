"""Real ASGI auth/nonce plus signed-client uncertainty checks on native SQL."""
import json
import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app.api import internal_nodes, internal_resource_reservations as api, internal_resource_inventory as control_api
from app.services.federation import protocol as p
from app.services.resource_peer_reservations import ReservationError
from app.services.resource_reservation_client import ReservationClient
from tests import test_resource_peer_reservations as fixtures


@unittest.skipUnless(os.name=='posix','native reservation requires Linux')
class ReservationAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=fixtures.PeerReservationTests('runTest');await self.f.asyncSetUp()
        self.peer=self.f.peer
        self.patches=[patch.object(internal_nodes,'state',self.peer.store),patch.object(api,'state',self.peer.store),
            patch.object(control_api,'state',self.peer.store),
            patch.object(api,'reservations',self.peer.reservations),patch.object(internal_nodes.settings,'TLS_ENABLED',True)]
        for item in self.patches:item.start()
        app=FastAPI();app.include_router(api.router)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://target.4399.com')

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):item.stop()
        await self.f.asyncTearDown()

    async def request(self,action,value,*,headers=None):
        body=p.canonical(value);path='/internal/v1/migration/reservations/'+action
        headers=p.auth_headers(self.peer.credential,self.peer.relationship,'POST',path,body) if headers is None else headers
        return await self.client.post(path,content=body,headers=headers)

    def payload(self):return dict(request_id=uuid.uuid4().hex,reservation=self.f.request)

    def verified(self,response):
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        return p.verify(p.public_key(self.peer.store.unseal(self.peer.store.node['private_key'])),response.json())

    async def test_signed_begin_and_nonce_replay_refusal_keep_capacity_once(self):
        payload=self.payload();path='/internal/v1/migration/reservations/begin'
        headers=p.auth_headers(self.peer.credential,self.peer.relationship,'POST',path,p.canonical(payload))
        value=self.verified(await self.request('begin',payload,headers=headers))
        self.assertEqual(value['request_id'],payload['request_id']);self.assertFalse(value['execution_authorized'])
        self.assertNotEqual((await self.request('begin',payload,headers=headers)).status_code,200)
        self.assertEqual(self.verified(await self.request('begin',self.payload()))['capacity_reserved'],123)
        self.assertEqual(await self.f.quota(),123)

    async def test_unsigned_forged_and_unknown_fields_cannot_reserve_or_echo_secrets(self):
        self.assertNotEqual((await self.request('begin',self.payload(),headers={})).status_code,200)
        response=await self.request('begin',{**self.payload(),'secret_key':'never_echo_this'})
        self.assertEqual(response.status_code,409);self.assertNotIn('never_echo_this',response.text)
        self.assertEqual(await self.f.quota(),0)

    async def test_unit_seal_abort_are_signed_controls_without_implicit_execution(self):
        self.verified(await self.request('begin',self.payload()))
        task=self.f.request['task_id']
        self.verified(await self.request('unit',dict(request_id=uuid.uuid4().hex,task_id=task,unit=self.f.units[0])))
        ready=self.verified(await self.request('seal',dict(request_id=uuid.uuid4().hex,task_id=task)))
        self.assertEqual(ready['state'],'prepared');self.assertFalse(ready['execution_authorized'])
        aborted=self.verified(await self.request('abort-unstarted',dict(request_id=uuid.uuid4().hex,task_id=task)))
        self.assertEqual(aborted['state'],'aborted');self.assertEqual(await self.f.quota(),0)

    async def test_missing_begin_cancellation_is_signed_and_fences_late_or_changed_begin(self):
        payload=self.payload();path='/internal/v1/migration/reservations/cancel-unstarted'
        headers=p.auth_headers(self.peer.credential,self.peer.relationship,'POST',path,p.canonical(payload))
        value=self.verified(await self.request('cancel-unstarted',payload,headers=headers))
        self.assertEqual(value['state'],'aborted');self.assertEqual(value['capacity_reserved'],0)
        self.assertEqual((await self.request('cancel-unstarted',payload,headers=headers)).status_code,401)
        self.assertEqual(self.verified(await self.request('begin',self.payload()))['state'],'aborted')
        changed={**self.f.request,'total_bytes':124}
        self.assertEqual((await self.request('begin',dict(request_id=uuid.uuid4().hex,reservation=changed))).status_code,409)
        self.assertEqual(await self.f.quota(),0)


@unittest.skipUnless(os.name=='posix','native reservation requires Linux')
class ReservationClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=fixtures.PeerReservationTests('runTest');await self.f.asyncSetUp()
        self.peer=self.f.peer
        self.tamper=None
        async with self.f.f.master.database.connect() as conn:
            from sqlalchemy import select
            from app.services.federation import schema as fs
            self.relation=dict((await conn.execute(select(fs.relationships).where(
                fs.relationships.c.peer_id==self.peer.store.node['node_id']))).mappings().one())
        self.client=ReservationClient(self.f.f.master.store,self.relation,SimpleNamespace(request=self.request),self.f.request)

    async def asyncTearDown(self):await self.f.asyncTearDown()

    async def request(self,origin,path,**kwargs):
        self.assertEqual(kwargs['credential'],self.peer.credential)
        self.assertEqual(origin,self.peer.store.node['endpoint'])
        action=path.rsplit('/',1)[-1];method={'unit':'add_unit','abort-unstarted':'abort_unstarted'}.get(action,action)
        result=await getattr(self.peer.reservations,method)(self.peer.store.node,await self.f.relation(),**kwargs['value'])
        if self.tamper is not None:result=self.tamper(result)
        return p.sign(self.peer.store.unseal(self.peer.store.node['private_key']),result)

    async def test_client_recovers_committed_preparation_after_bad_or_lost_reply(self):
        for change in (dict(request_id='0'*32),dict(capacity_reserved=0),dict(cancelled_bytes=True),dict(cancelled_bytes=124),dict(state=[]),dict(unit_count=True),dict(padding='x'*9000)):
            self.tamper=lambda row,change=change:{**row,**change}
            with self.assertRaises(ReservationError):await self.client.begin()
            self.assertEqual(await self.f.quota(),123)
        self.tamper=None
        status=await self.client.begin();self.assertEqual(status['capacity_reserved'],123)
        await self.client.add_unit(self.f.units[0]);self.assertEqual((await self.client.seal())['state'],'prepared')
        self.assertEqual((await self.client.abort_unstarted())['state'],'aborted')
        self.assertEqual(await self.f.quota(),0)
