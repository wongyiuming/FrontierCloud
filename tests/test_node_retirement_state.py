"""Durable preparation and ordinary-write fences in two actual SQLite stores."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import insert,select,text,update

from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.services import node_retirement_schema as s,resource_fences
from app.services.federation import protocol as p,schema as fs
from app.services.federation.state import State
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_gate import Gate
from app.services.node_retirement_state import Journal


@unittest.skipUnless(os.name=='posix','native root binding requires Linux')
class RetirementStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name);self.nodes=[]
        self.relationship='a'*32;self.credential=p.encode(os.urandom(48));self.now=1000
        for name,role in (('master','Master'),('peer','Follower')):
            root=self.root/name;root.mkdir();database=create_sqlite_engine('sqlite+aiosqlite:///'+str(root/'state.db'))
            with patch.object(db,'engine',database),patch.object(db,'SECRET_DIR',root/'secrets'):await db.init_db()
            store=State(database,Fernet.generate_key());await store.initialize()
            async with database.begin() as conn:await conn.execute(update(fs.identity).values(role=role,endpoint=f'https://{name}.4399.com'))
            store.node.update(role=role,endpoint=f'https://{name}.4399.com');self.nodes.append(store)
        self.master,self.peer=self.nodes;self.gate=Gate(self.root/'peer')
        self.master_journal=Journal(self.master,wall=lambda:self.now);self.peer_journal=Journal(self.peer,gate=self.gate,wall=lambda:self.now)
        for local,remote,direction in ((self.master,self.peer,'downstream'),(self.peer,self.master,'upstream')):
            async with local.database.begin() as conn:
                await conn.execute(insert(fs.relationships).values(relationship_id=self.relationship,peer_id=remote.node['node_id'],
                    peer_endpoint=remote.node['endpoint'],peer_key=p.public_key(remote.unseal(remote.node['private_key'])),
                    credential=local.seal(self.credential),direction=direction,mode='Direct',state='active',status='online',last_heartbeat=999,
                    rtt_ms=1,failures=0,recoveries=0,peer_version=p.APP_VERSION,protocol=p.PROTOCOL_VERSION,summary={},created_at=1))
                await conn.execute(insert(fs.storage_members).values(member_id=self.peer.node['node_id'],relationship_id=self.relationship if local is self.master else None,
                    member_kind='Follower',transport='Direct' if local is self.master else 'Local',storage_enabled=1,allocated_bytes=1024**3,used_bytes=0,reserved_bytes=0,
                    physical_free_bytes=1024**3,health='online',writable=1,updated_at=1))
        self.original=self.root/'peer/unknown-media';self.original.write_bytes(b'never remove unknown files')
        self.identities=[dict(store.node) for store in self.nodes]

    async def asyncTearDown(self):
        self.gate.close()
        for store in self.nodes:await store.database.dispose()
        self.temporary.cleanup()

    async def begin(self):return await self.master_journal.begin_master(self.relationship,self.peer.node['node_id'],'test-admin')

    async def both(self):
        master=await self.begin();peer=await self.peer_journal.begin_peer(self.relationship,master['binding']);return master,peer

    async def unchanged(self):
        self.assertEqual(self.original.read_bytes(),b'never remove unknown files')
        for store,before in zip(self.nodes,self.identities):
            async with store.database.connect() as conn:
                actual=dict((await conn.execute(select(fs.identity))).mappings().one());self.assertEqual(actual,before)

    async def test_lost_ack_and_new_journal_replay_one_operation_one_audit_and_timestamp(self):
        first,peer=await self.both();self.now+=99999
        master_again=await Journal(self.master,wall=lambda:self.now).begin_master(self.relationship,self.peer.node['node_id'],'retry')
        gate=Gate(self.root/'peer')
        try:peer_again=await Journal(self.peer,gate=gate,wall=lambda:self.now).begin_peer(self.relationship,first['binding'])
        finally:gate.close()
        self.assertEqual(first,master_again);self.assertEqual(peer,peer_again)
        self.assertEqual(first['binding'],peer['binding']);self.assertEqual(first['authorization_sha256'],peer['authorization_sha256'])
        for store in self.nodes:
            async with store.database.connect() as conn:
                self.assertEqual(await conn.scalar(select(fs.relationships.c.state)),'retiring')
                self.assertEqual(await conn.scalar(select(fs.storage_members.c.storage_enabled)),0)
                self.assertEqual(await conn.scalar(select(fs.storage_members.c.writable)),0)
                self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM node_audit WHERE action='retirement-started'")),1)
        self.assertEqual(first['phase'],'retiring');self.assertEqual(peer['receipt'],{});await self.unchanged()

    async def test_wrong_node_confirmation_has_no_side_effects(self):
        with self.assertRaisesRegex(RetirementError,'retirement_confirmation_mismatch'):
            await self.master_journal.begin_master(self.relationship,self.master.node['node_id'],'test')
        async with self.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM node_retirements')),0)
            self.assertEqual(await conn.scalar(select(fs.relationships.c.state)),'active')
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.storage_enabled)),1)
        await self.unchanged()

    async def test_peer_rejects_changed_identity_key_endpoint_mode_and_operation_binding(self):
        master=await self.begin()
        for field,value in (('member_id','b'*32),('master_id','c'*32),('master_key_sha256','d'*64),('member_endpoint','https://other.4399.com'),('mode','Relay'),('protocol',999)):
            changed={**master['binding'],field:value}
            with self.subTest(field=field),self.assertRaisesRegex(RetirementError,'retirement_binding_changed'):
                await self.peer_journal.begin_peer(self.relationship,changed)
        await self.peer_journal.begin_peer(self.relationship,master['binding'])
        changed={**master['binding'],'retirement_id':'e'*32}
        with self.assertRaisesRegex(RetirementError,'retirement_operation_conflict'):
            await self.peer_journal.begin_peer(self.relationship,changed)
        await self.unchanged()

    async def test_same_old_credential_cannot_authorize_ordinary_requests_after_fence(self):
        await self.both()
        for store in self.nodes:
            body=b'{}';path='/internal/v1/heartbeat';headers={key.lower():value for key,value in p.auth_headers(self.credential,self.relationship,'POST',path,body).items()}
            with self.assertRaisesRegex(p.ProtocolError,'Relationship not active'):
                await store.authenticate(headers,'POST',path,body)
        await self.unchanged()

    async def test_durable_fence_still_denies_auth_if_a_stale_relation_row_says_active(self):
        await self.both()
        for store in self.nodes:
            async with store.database.begin() as conn:await conn.execute(update(fs.relationships).values(state='active'))
            body=b'{}';path='/internal/v1/heartbeat';headers={key.lower():value for key,value in p.auth_headers(self.credential,self.relationship,'POST',path,body).items()}
            with self.assertRaisesRegex(RetirementError,'retirement_identity_fenced'):
                await store.authenticate(headers,'POST',path,body)
            async with store.database.connect() as conn:
                self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM node_request_nonces')),0)
        await self.unchanged()

    async def test_new_upload_recording_config_reset_repair_and_legacy_revoke_cannot_remove_fence(self):
        await self.both()
        for store in self.nodes:
            for operation in ('upload','recording','config','reset'):
                async with store.database.begin() as conn:
                    with self.subTest(role=store.node['role'],operation=operation),self.assertRaises(RetirementError):
                        if operation=='upload':await resource_fences.new_upload(conn,self.peer.node['node_id'],'media','music/album/new.mp3')
                        elif operation=='recording':await resource_fences.recording(conn,self.peer.node['node_id'],'f'*32,upload=True)
                        elif operation=='config':await resource_fences.member_clear(conn,self.peer.node['node_id'])
                        else:await resource_fences.node_clear(conn)
            with self.assertRaises(RetirementError):await store.activate(self.relationship,'legacy-confirm')
            with self.assertRaises(RetirementError):await store.revoke(self.relationship,'legacy-revoke',peer_confirmed=True)
        with self.assertRaises(RetirementError):await self.peer.create_pair('old-identity')
        await self.unchanged()

    async def test_already_admitted_recording_returns_conflict_without_reading_body_or_creating_payload(self):
        from app.services import karaoke_storage
        await self.both();root=self.root/'peer/recordings';root.mkdir()
        class Request:
            async def stream(self):
                raise AssertionError('fenced recording body was consumed')
                yield b''
        relation=dict(relationship_id=self.relationship,allocated_capacity_bytes=1024**3)
        value=dict(u='f'*32,i='e'*32,size=5)
        with patch.object(karaoke_storage,'state',self.peer),patch.object(karaoke_storage,'ROOT',root):
            with self.assertRaises(HTTPException) as error:await karaoke_storage.receive(Request(),relation,value)
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(list(root.iterdir()),[])
        async with self.peer.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.reserved_bytes)),0)
        await self.unchanged()

    async def test_retiring_one_member_does_not_block_master_local_or_unrelated_member(self):
        await self.both()
        async with self.master.database.begin() as conn:
            for member in (self.master.node['node_id'],'f'*32):
                await resource_fences.new_upload(conn,member,'media','music/unrelated/new.mp3')
                await resource_fences.recording(conn,member,'f'*32,upload=True)
            await resource_fences.paths(conn,'media',[('music/unrelated/new.mp3',False)])
        async with self.peer.database.begin() as conn:
            with self.assertRaises(RetirementError):await resource_fences.paths(conn,'media',[('music/album/new.mp3',False)])

    async def test_nonzero_reservations_and_retained_backup_block_before_any_fence_is_written(self):
        async with self.master.database.begin() as conn:await conn.execute(update(fs.storage_members).values(reserved_bytes=1))
        with self.assertRaisesRegex(RetirementError,'retirement_member_usage'):await self.begin()
        async with self.master.database.begin() as conn:
            await conn.execute(update(fs.storage_members).values(reserved_bytes=0))
            await conn.execute(insert(fs.backup_members).values(member_id=self.peer.node['node_id'],enabled=0,generation=7,last_success=1,lag_seconds=0,checksum='a'*64,state='disabled',updated_at=1))
        with self.assertRaisesRegex(RetirementError,'retirement_backup_retained'):await self.begin()
        async with self.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.relationships.c.state)),'active')
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM node_retirements')),0)
        await self.unchanged()

    async def test_audit_failure_rolls_back_relationship_and_disabled_storage_with_journal(self):
        async def fail(*_args,**_kwargs):raise RuntimeError('controlled audit write failure')
        with patch.object(self.master,'log',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'controlled audit'):await self.begin()
        async with self.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.relationships.c.state)),'active')
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.writable)),1)
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM node_retirements')),0)
        await self.begin();await self.unchanged()

    async def test_peer_audit_failure_retains_master_fence_and_retries_the_same_operation(self):
        master=await self.begin()
        async def fail(*_args,**_kwargs):raise RuntimeError('controlled peer audit failure')
        with patch.object(self.peer,'log',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'controlled peer audit'):
                await self.peer_journal.begin_peer(self.relationship,master['binding'])
        async with self.peer.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.relationships.c.state)),'active')
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM node_retirements')),0)
        retained=await self.master_journal.read(self.relationship);self.assertEqual(retained,master)
        peer=await self.peer_journal.begin_peer(self.relationship,master['binding'])
        self.assertEqual(peer['retirement_id'],master['retirement_id']);await self.unchanged()

    async def test_expired_reserved_upload_is_a_blocker_until_its_own_lifecycle_settles(self):
        async with self.master.database.begin() as conn:
            await conn.execute(insert(fs.upload_sessions).values(upload_id='b'*32,storage_member_id=self.peer.node['node_id'],
                media_id='c'*64,media_path='music/album/original.mp3',path_locator='d'*64,object_kind='audio',expected_bytes=123,
                content_encrypted=1,state='reserved',expires_at=1,created_at=1,updated_at=1))
        with self.assertRaisesRegex(RetirementError,'retirement_unfinished_upload'):await self.begin()
        async with self.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.upload_sessions.c.state)),'reserved')
            self.assertEqual(await conn.scalar(select(fs.relationships.c.state)),'active')
        await self.unchanged()

    async def test_orphan_native_backup_chunk_is_retained_and_blocks_peer_preparation(self):
        master=await self.begin()
        async with self.peer.database.begin() as conn:
            await conn.execute(insert(fs.business_backup_chunks).values(master_id=self.master.node['node_id'],generation=7,chunk_index=0,payload=b'old recovery bytes',created_at=1))
        with self.assertRaisesRegex(RetirementError,'retirement_backup_retained'):
            await self.peer_journal.begin_peer(self.relationship,master['binding'])
        async with self.peer.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.business_backup_chunks.c.payload)),b'old recovery bytes')
            self.assertEqual(await conn.scalar(select(fs.relationships.c.state)),'active')
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM node_retirements')),0)
        await self.unchanged()

    async def test_changed_credential_after_restart_cannot_rebind_the_old_retirement(self):
        await self.both()
        async with self.peer.database.begin() as conn:await conn.execute(update(fs.relationships).values(credential=self.peer.seal(p.encode(os.urandom(48)))))
        with self.assertRaisesRegex(RetirementError,'retirement_identity_changed'):await self.peer_journal.read(self.relationship)
        await self.unchanged()

    async def test_replaced_root_gate_and_corrupt_binding_are_preserved_and_refused(self):
        await self.both();self.gate.close()
        from app.services.node_retirement_gate import NAME
        path=self.root/'peer'/NAME;path.rename(path.with_name('retained-old-gate'))
        self.gate=Gate(self.root/'peer')
        with self.assertRaisesRegex(RetirementError,'retirement_roots_changed'):
            await Journal(self.peer,gate=self.gate).read(self.relationship)
        async with self.master.database.begin() as conn:await conn.execute(update(s.retirements).values(binding_json='{}'))
        with self.assertRaisesRegex(RetirementError,'retirement_journal_invalid'):await self.master_journal.read(self.relationship)
        self.assertTrue(path.with_name('retained-old-gate').exists());await self.unchanged()
