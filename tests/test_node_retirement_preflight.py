"""Readonly real-native observations behind the exact authenticated ASGI route."""
import copy
import time
import uuid
from unittest.mock import patch

from sqlalchemy import insert,select,update

from app.services import node_retirement_protocol as protocol,node_retirement_schema as s,resource_migration_schema as migrations
from app.services.federation import protocol as p,schema as fs
from app.services.node_retirement_client import Client,Coordinator
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_preflight import Preflight
from app.services.node_retirement_state import binding
from tests.test_node_retirement_api import RetirementAPIFixture


class RetirementPreflightTests(RetirementAPIFixture):
    async def client_binding(self):
        relation=await self.f.master.relationship(self.f.relationship)
        value,_=binding(self.f.master.node,relation,self.f.master,uuid.uuid4().hex)
        return Client(self.f.master,relation,self.transport),value

    async def snapshot(self):
        result=[]
        for store in (self.f.master,self.f.peer):
            async with store.database.connect() as conn:
                result.append([list((await conn.execute(select(table))).mappings()) for table in
                    (fs.identity,fs.relationships,fs.storage_members,s.retirements,fs.audit)])
        return copy.deepcopy(result)

    async def test_successful_repeated_preflight_preserves_all_business_rows_and_files(self):
        before=await self.snapshot();vault=self.f.file.vault.read_bytes();inode=self.f.file.vault.stat().st_ino
        for _ in range(2):
            report=await Preflight(self.f.master_journal).master(self.f.relationship,self.transport)
            self.assertTrue(report['ready']);self.assertTrue(report['preflight_only'])
            self.assertEqual(report['master']['history'],dict(master_tasks=0))
            observation=report['peer']['observation'];self.assertEqual(observation['history'],dict(native_tasks=0))
            self.assertGreater(observation['inspection']['retained_files'],0)
            self.assertNotIn(str(self.f.file.base),str(report))
        self.assertEqual(await self.snapshot(),before);self.assertEqual(await self.nonce_count(),2)
        self.assertEqual(self.f.file.vault.read_bytes(),vault);self.assertEqual(self.f.file.vault.stat().st_ino,inode)
        # Readonly success is not a retirement fence: ordinary health still works.
        path='/internal/v1/heartbeat';raw=b'{}'
        self.assertEqual((await self.http.post(path,content=raw,headers=self.headers(raw,path=path))).status_code,200)

    async def test_unknown_file_is_reported_without_mutation_or_preparation(self):
        path=self.f.file.paths['media']/'unknown.bin';path.write_bytes(b'keep unknown ciphertext');inode=path.stat().st_ino
        before=await self.snapshot();report=await Preflight(self.f.master_journal).master(self.f.relationship,self.transport)
        self.assertFalse(report['ready']);self.assertEqual(report['master']['state'],'passed')
        self.assertEqual(report['peer'],dict(state='blocked',code='retirement_unknown_file'))
        self.assertEqual(await self.snapshot(),before);self.assertEqual(path.read_bytes(),b'keep unknown ciphertext');self.assertEqual(path.stat().st_ino,inode)

    async def test_master_and_peer_blockers_are_observed_independently(self):
        for store in (self.f.master,self.f.peer):
            async with store.database.begin() as conn:await conn.execute(update(fs.storage_members).values(reserved_bytes=1))
        before=await self.snapshot();report=await Preflight(self.f.master_journal).master(self.f.relationship,self.transport)
        self.assertFalse(report['ready'])
        for side in ('master','peer'):self.assertEqual(report[side],dict(state='blocked',code='retirement_member_usage'))
        self.assertEqual(await self.snapshot(),before)

    async def test_native_shared_request_returns_signed_busy_and_keeps_no_journal(self):
        with self.f.file.runtime.gate.lease():
            report=await Preflight(self.f.master_journal).master(self.f.relationship,self.transport)
        self.assertFalse(report['ready']);self.assertEqual(report['peer']['code'],'retirement_requests_busy')
        async with self.f.peer.database.connect() as conn:self.assertIsNone(await conn.scalar(select(s.retirements.c.retirement_id)))

    async def test_migration_reservation_is_a_specific_signed_blocker(self):
        async with self.f.peer.database.begin() as conn:
            await conn.execute(insert(migrations.drains).values(member_id=self.f.peer.node['node_id'],owner_id='e'*32,reason='administrator',created_at=int(time.time())))
        report=await Preflight(self.f.master_journal).master(self.f.relationship,self.transport)
        self.assertFalse(report['ready']);self.assertEqual(report['peer']['code'],'migration_member_busy')

    async def test_no_retirement_authority_or_missing_gateway_observation_is_not_success(self):
        authority=self.f.file.runtime.authority;self.f.file.runtime.authority=None
        try:report=await Preflight(self.f.master_journal).master(self.f.relationship,self.transport)
        finally:self.f.file.runtime.authority=authority
        self.assertEqual(report['peer']['code'],'retirement_native_observation_unavailable')
        self.f.traffic._required=True
        report=await Preflight(self.f.master_journal).master(self.f.relationship,self.transport)
        self.assertFalse(report['ready']);self.assertEqual(report['peer']['code'],'retirement_reader_observation_unavailable')

    async def test_transport_loss_is_unverified_and_does_not_create_a_journal(self):
        with patch.object(self.transport,'request',side_effect=TimeoutError('private connection text')):
            report=await Preflight(self.f.master_journal).master(self.f.relationship,self.transport)
        self.assertEqual(report['peer'],dict(state='unverified',code='retirement_peer_unavailable'))
        self.assertNotIn('private connection text',str(report));self.assertFalse(report['ready'])

    async def test_preflight_does_not_bypass_already_prepared_or_completed_peers(self):
        await self.prepare();await self.client.call('begin',self.proposal['binding'])
        self.assertEqual((await self.send(self.value('check'))).status_code,401)
        with self.assertRaisesRegex(RetirementError,'retirement_already_started'):
            await Preflight(self.f.master_journal).master(self.f.relationship,self.transport)
        await Coordinator(self.f.master_journal,self.transport).advance(self.f.relationship,self.f.peer.node['node_id'],'test-admin')
        self.assertEqual((await self.send(self.value('check'))).status_code,401)

    async def test_preflight_signed_reply_cannot_be_reused_or_misbound(self):
        client,value=await self.client_binding();saved=[];original=self.transport.request
        async def capture(*args,**kwargs):
            result=await original(*args,**kwargs);saved.append(result);return result
        with patch.object(self.transport,'request',side_effect=capture):await client.call('check',value)
        with patch.object(self.transport,'request',return_value=saved[0]):
            with self.assertRaisesRegex(RetirementError,'retirement_signed_preflight_invalid'):await client.call('check',value)

    async def test_signed_preflight_boolean_counts_stale_time_and_extra_fields_are_refused(self):
        _,value=await self.client_binding()
        request=dict(format=protocol.FORMAT,version=1,action='check',request_id=uuid.uuid4().hex,binding=value)
        response=await self.send(request);self.assertEqual(response.status_code,200)
        payload=p.verify(p.public_key(self.f.peer.unseal(self.f.peer.node['private_key'])),response.json())
        variants=[{**payload,'observed_at':int(time.time())-61},{**payload,'observed_at':True},
            {**payload,'history':{'native_tasks':False}},{**payload,'inspection':{**payload['inspection'],'retained_bytes':True}},
            {**payload,'extra':'not allowed'}]
        for invalid in variants:
            envelope=p.sign(self.f.peer.unseal(self.f.peer.node['private_key']),invalid)
            with self.assertRaisesRegex(RetirementError,'retirement_signed_preflight_invalid'):
                protocol.verify_preflight(request,envelope,p.public_key(self.f.peer.unseal(self.f.peer.node['private_key'])),value,time.time())

    async def test_preflight_success_does_not_authorize_later_changed_files(self):
        report=await Preflight(self.f.master_journal).master(self.f.relationship,self.transport);self.assertTrue(report['ready'])
        path=self.f.file.paths['media']/'later.bin';path.write_bytes(b'preserve later bytes')
        await self.prepare()
        with self.assertRaisesRegex(RetirementError,'retirement_unknown_file'):
            await Coordinator(self.f.master_journal,self.transport).advance(self.f.relationship,self.f.peer.node['node_id'],'test-admin')
        self.assertEqual(path.read_bytes(),b'preserve later bytes');await self.f.phase(self.f.peer,'retiring')
