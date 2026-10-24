"""Two actual stores and pinned native files; terminal core, without HTTP auth."""
import asyncio
import copy
import os
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import insert,select,text,update

from app.core import db
from app.core.config import settings
from app.core.sqlite_compat import create_sqlite_engine
from app.services import business_traffic,media_crypto,node_retirement_schema as s,resource_fences,resource_migration_schema as migrations,resource_pool,runtime_gate
from app.services.federation import protocol as p,schema as fs
from app.services.federation.state import State
from app.services.node_retirement_complete import Completion
from app.services.node_retirement_files import Census
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_gate import Gate,GateError
from app.services.node_retirement_runtime import Runtime
from app.services.node_retirement_state import Journal
from tests import test_node_retirement_files as files


@unittest.skipUnless(os.name=='posix','terminal retirement requires native Linux runtime ownership')
class RetirementCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mode=patch.object(settings,'DEPLOYMENT_MODE','only_stroge');self.mode.start()
        self.file=files.RetirementFilesTests('runTest');await self.file.asyncSetUp()
        self.peer=self.file.store;self.file.runtime.authority=self.file.authority;self.now=int(time.time())
        root=self.file.base/'master';root.mkdir()
        self.database=create_sqlite_engine('sqlite+aiosqlite:///'+str(root/'state.db'))
        with patch.object(db,'engine',self.database),patch.object(db,'SECRET_DIR',root/'secrets'):await db.init_db()
        self.master=State(self.database,Fernet.generate_key());await self.master.initialize()
        async with self.database.begin() as conn:await conn.execute(update(fs.identity).values(role='Master',endpoint='https://master.4399.com'))
        self.master.node.update(role='Master',endpoint='https://master.4399.com')
        self.relationship='c'*32;self.credential=p.encode(os.urandom(48));self.traffic=business_traffic.Monitor()
        for local,remote,direction in ((self.master,self.peer,'downstream'),(self.peer,self.master,'upstream')):
            async with local.database.begin() as conn:
                await conn.execute(insert(fs.relationships).values(relationship_id=self.relationship,peer_id=remote.node['node_id'],
                    peer_endpoint=remote.node['endpoint'],peer_key=p.public_key(remote.unseal(remote.node['private_key'])),
                    credential=local.seal(self.credential),direction=direction,mode='Direct',state='active',status='online',last_heartbeat=self.now,
                    rtt_ms=1,failures=0,recoveries=0,peer_version=p.APP_VERSION,protocol=p.PROTOCOL_VERSION,summary={'existing_note':'retained'},created_at=self.now))
        async with self.database.begin() as conn:
            await conn.execute(insert(fs.storage_members).values(member_id=self.peer.node['node_id'],relationship_id=self.relationship,
                member_kind='Follower',transport='Direct',storage_enabled=1,allocated_bytes=1024**3,used_bytes=0,reserved_bytes=0,
                physical_free_bytes=1024**3,health='online',writable=1,updated_at=self.now))
        from app.api.v1 import media
        with patch.object(media,'MEDIA_ROOT',self.file.paths['media']):
            await resource_pool.accept_follower_configuration(dict(storage=dict(enabled=True,allocated_bytes=1024**3),
                backup=dict(enabled=False)),self.peer.node,self.peer.database)
        self.master_journal=Journal(self.master,wall=lambda:self.now)
        self.bind_peer()
        self.master_complete=Completion(self.master_journal)
        self.identities=[dict(store.node) for store in (self.master,self.peer)]

    async def asyncTearDown(self):
        await self.database.dispose();await self.file.asyncTearDown();self.mode.stop()

    def bind_peer(self):
        self.peer_journal=Journal(self.peer,gate=self.file.runtime.gate,authority=self.file.authority,wall=lambda:self.now)
        self.peer_complete=Completion(self.peer_journal,runtime=self.file.runtime,traffic=self.traffic)

    async def begin(self):
        self.proposal=await self.master_journal.begin_master(self.relationship,self.peer.node['node_id'],'test-admin')
        return await self.peer_journal.begin_peer(self.relationship,self.proposal['binding'])

    async def peer_done(self):
        with self.file.runtime.gate.lease(exclusive=True) as lease:
            return await self.peer_complete.peer(self.relationship,self.proposal['binding'],lease)

    async def master_done(self,receipt):
        return await self.master_complete.master(self.relationship,self.peer.node['node_id'],receipt,'test-admin')

    async def phase(self,store,expected):
        async with store.database.connect() as conn:
            row=dict((await conn.execute(select(s.retirements))).mappings().one())
            relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            member=dict((await conn.execute(select(fs.storage_members))).mappings().one())
            self.assertEqual(row['phase'],expected);self.assertEqual(relation['state'],'revoked' if expected=='completed' else 'retiring')
            self.assertEqual((member['storage_enabled'],member['writable'],member['used_bytes'],member['reserved_bytes']),(0,0,0,0))
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM node_audit WHERE action='retirement-completed'")),int(expected=='completed'))
            if expected=='completed':
                self.assertTrue(relation['summary']['revocation_acknowledged']);self.assertEqual(relation['summary']['existing_note'],'retained')

    async def test_both_terminal_receipts_preserve_identity_keys_and_are_idempotent(self):
        await self.begin();vault=self.file.vault.read_bytes();inode=self.file.vault.stat().st_ino
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('opened media key')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster'))):
            peer=await self.peer_done();master=await self.master_done(peer['receipt'])
        self.assertEqual(peer['receipt']['payload']['history'],dict(native_tasks=0))
        self.assertEqual(master['receipt']['payload']['history'],dict(master_tasks=0))
        self.assertEqual(master['receipt']['payload']['peer_receipt'],peer['receipt'])
        self.now+=100000
        self.assertEqual(await self.peer_done(),peer);self.assertEqual(await self.master_done(peer['receipt']),master)
        self.assertEqual(await self.master_done(None),master)
        for store,before in zip((self.master,self.peer),self.identities):
            await self.phase(store,'completed')
            async with store.database.connect() as conn:self.assertEqual(dict((await conn.execute(select(fs.identity))).mappings().one()),before)
        self.assertEqual(self.file.vault.read_bytes(),vault);self.assertEqual(self.file.vault.stat().st_ino,inode)

    async def test_unknown_file_after_preparation_blocks_and_is_preserved(self):
        await self.begin();path=self.file.paths['media']/'unknown.bin';path.write_bytes(b'unknown original ciphertext');inode=path.stat().st_ino
        with self.assertRaisesRegex(RetirementError,'retirement_unknown_file'):await self.peer_done()
        self.assertEqual(path.read_bytes(),b'unknown original ciphertext');self.assertEqual(path.stat().st_ino,inode)
        await self.phase(self.peer,'retiring');await self.phase(self.master,'retiring')

    async def test_relay_uses_the_same_native_local_storage_and_terminal_contract(self):
        await self.master.set_mode(self.relationship,'Relay','test-admin')
        await self.peer.accept_mode(self.relationship,'Relay',self.master.node['node_id'])
        await self.begin();peer=await self.peer_done();await self.master_done(peer['receipt'])
        await self.phase(self.peer,'completed');await self.phase(self.master,'completed')

    async def test_master_style_member_binding_on_native_peer_is_refused(self):
        async with self.peer.database.begin() as conn:await conn.execute(update(fs.storage_members).values(relationship_id=self.relationship))
        with self.assertRaisesRegex(RetirementError,'retirement_member_binding_changed'):await self.begin()
        async with self.peer.database.connect() as conn:
            self.assertIsNone(await conn.scalar(select(s.retirements.c.retirement_id)))
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.relationship_id)),self.relationship)

    async def test_late_reader_after_terminal_sql_writes_rolls_back_completion(self):
        await self.begin();original=self.peer_complete._write
        async def late(*args):
            value=await original(*args);self.traffic.entered();return value
        try:
            with patch.object(self.peer_complete,'_write',side_effect=late):
                with self.assertRaisesRegex(RetirementError,'retirement_readers_active'):await self.peer_done()
        finally:self.traffic.exited()
        await self.phase(self.peer,'retiring');await self.peer_done();await self.phase(self.peer,'completed')

    async def test_late_expired_upload_remains_a_logical_blocker(self):
        await self.begin()
        async with self.peer.database.begin() as conn:
            await conn.execute(insert(fs.upload_sessions).values(upload_id='1'*32,storage_member_id=self.peer.node['node_id'],
                media_id='2'*64,media_path='music/album/original.mp3',path_locator='3'*64,object_kind='audio',expected_bytes=123,
                content_encrypted=1,state='reserved',expires_at=1,created_at=1,updated_at=1))
        self.now+=4000
        with self.assertRaisesRegex(RetirementError,'retirement_unfinished_upload'):await self.peer_done()
        async with self.peer.database.connect() as conn:self.assertEqual(await conn.scalar(select(fs.upload_sessions.c.state)),'reserved')
        await self.phase(self.peer,'retiring')

    async def test_orphan_completed_migration_unit_is_not_an_empty_node(self):
        await self.begin()
        async with self.peer.database.begin() as conn:
            await conn.execute(insert(migrations.units).values(task_id='1'*32,unit_id='2'*64,resource_kind='media',parent_path='music/album',
                source_version='3'*64,state='cancelled',object_count=1,total_bytes=1,publication={}))
        with self.assertRaisesRegex(RetirementError,'retirement_migration_orphan'):await self.peer_done()
        await self.phase(self.peer,'retiring')

    async def test_shared_foreign_or_gate_only_capabilities_cannot_complete(self):
        await self.begin()
        with self.file.runtime.gate.lease() as lease:
            with self.assertRaisesRegex(GateError,'retirement_lease_invalid'):
                await self.peer_complete.peer(self.relationship,self.proposal['binding'],lease)
        other=Gate(self.file.data)
        try:
            with other.lease(exclusive=True) as lease:
                with self.assertRaisesRegex(GateError,'retirement_lease_invalid'):
                    await self.peer_complete.peer(self.relationship,self.proposal['binding'],lease)
        finally:other.close()
        plain=Completion(Journal(self.peer,gate=self.file.runtime.gate),runtime=self.file.runtime,traffic=self.traffic)
        with self.file.runtime.gate.lease(exclusive=True) as lease:
            with self.assertRaisesRegex(RetirementError,'retirement_native_authority_required'):
                await plain.peer(self.relationship,self.proposal['binding'],lease)
        await self.phase(self.peer,'retiring')

    async def test_changed_vault_is_not_regenerated_or_deleted(self):
        await self.begin();old=self.file.vault.with_name('retained-original-vault');self.file.vault.rename(old)
        replacement=Fernet.generate_key();self.file.vault.write_bytes(replacement);self.file.vault.chmod(0o600)
        with self.assertRaisesRegex(RetirementError,'retirement_control_file_changed'):await self.peer_done()
        self.assertEqual(old.read_bytes(),self.file.original_key);self.assertEqual(self.file.vault.read_bytes(),replacement)
        await self.phase(self.peer,'retiring')

    async def test_peer_audit_failure_rolls_back_terminal_state_but_keeps_fence(self):
        await self.begin()
        async def fail(*_args,**_kwargs):raise RuntimeError('controlled terminal audit failure')
        with patch.object(self.peer,'log',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'controlled terminal audit'):await self.peer_done()
        await self.phase(self.peer,'retiring');self.now+=1
        await self.peer_done();await self.phase(self.peer,'completed')

    async def test_master_audit_failure_keeps_peer_receipt_and_retry_finishes_once(self):
        await self.begin();peer=await self.peer_done()
        async def fail(*_args,**_kwargs):raise RuntimeError('controlled master terminal audit failure')
        with patch.object(self.master,'log',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'controlled master terminal audit'):await self.master_done(peer['receipt'])
        await self.phase(self.master,'retiring');await self.phase(self.peer,'completed')
        self.now+=2;await self.master_done(peer['receipt']);await self.phase(self.master,'completed')
        self.assertEqual(await self.peer_done(),peer)

    async def test_wrong_confirmation_or_forged_receipts_cannot_revoke_master(self):
        await self.begin();peer=await self.peer_done()
        with self.assertRaisesRegex(RetirementError,'retirement_confirmation_mismatch'):
            await self.master_complete.master(self.relationship,self.master.node['node_id'],peer['receipt'],'test')
        changed=copy.deepcopy(peer['receipt']);changed['payload']['inspection']['retained_files']+=1
        with self.assertRaisesRegex(RetirementError,'retirement_receipt_invalid'):await self.master_done(changed)
        changed=copy.deepcopy(peer['receipt']['payload']);changed['retirement_id']='f'*32
        with self.assertRaisesRegex(RetirementError,'retirement_receipt_invalid'):
            await self.master_done(p.sign(self.peer.unseal(self.peer.node['private_key']),changed))
        await self.phase(self.master,'retiring')

    async def test_completed_journal_reads_validate_signature_and_timestamp(self):
        await self.begin();peer=await self.peer_done();await self.master_done(peer['receipt'])
        altered=copy.deepcopy(peer['receipt']);altered['payload']['completed_at']+=1
        async with self.peer.database.begin() as conn:await conn.execute(update(s.retirements).values(receipt_json=p.canonical(altered).decode()))
        with self.assertRaisesRegex(RetirementError,'retirement_receipt_invalid'):await self.peer_journal.read(self.relationship)

    async def test_completed_peer_stays_fenced_until_explicit_identity_reset(self):
        await self.begin();await self.peer_done()
        async with self.peer.database.begin() as conn:
            with self.assertRaises(RetirementError):await resource_fences.new_upload(conn,self.peer.node['node_id'],'media','music/album/new.mp3')
            await resource_fences.node_clear(conn)
        path='/internal/v1/heartbeat';raw=b'{}'
        with self.assertRaises(p.ProtocolError):
            await self.peer.authenticate(p.auth_headers(self.credential,self.relationship,'POST',path,raw),'POST',path,raw)

    async def test_active_and_unavailable_reader_observations_keep_retirement_fenced(self):
        await self.begin();self.traffic.entered()
        with self.assertRaisesRegex(RetirementError,'retirement_readers_active'):await self.peer_done()
        self.traffic.exited();self.traffic.unavailable('controlled missing observer')
        with self.assertRaisesRegex(RetirementError,'retirement_reader_observation_unavailable'):await self.peer_done()
        await self.phase(self.peer,'retiring')

    async def test_nginx_requires_a_new_sample_after_exclusive_admission(self):
        await self.begin();self.traffic.require_gateway(True)
        self.traffic.update_gateway(business_traffic.GatewaySample('1-1',int(time.time()*1000),0,0,0,0))
        with patch.object(settings,'DEPLOYMENT_MODE','only_business'):
            with self.assertRaisesRegex(RetirementError,'retirement_reader_observation_(unavailable|not_fresh)'):await self.peer_done()
        await self.phase(self.peer,'retiring')

    async def test_fresh_gateway_active_sample_blocks_and_fresh_zero_sample_completes(self):
        await self.begin();self.traffic.require_gateway(True)
        self.traffic.update_gateway(business_traffic.GatewaySample('1-1',int(time.time()*1000),0,0,0,0))
        stopped=asyncio.Event()
        async def publish(active):
            while not stopped.is_set():
                await asyncio.sleep(.1);self.traffic.update_gateway(business_traffic.GatewaySample('1-1',int(time.time()*1000),0,0,0,active))
        with patch.object(settings,'DEPLOYMENT_MODE','only_business'):
            task=asyncio.create_task(publish(1))
            try:
                with self.assertRaisesRegex(RetirementError,'retirement_readers_active'):await self.peer_done()
            finally:stopped.set();await task
            await self.phase(self.peer,'retiring')
            self.traffic.update_gateway(business_traffic.GatewaySample('1-2',int(time.time()*1000),0,0,0,0))
            stopped.clear();task=asyncio.create_task(publish(0))
            try:await self.peer_done()
            finally:stopped.set();await task
        await self.phase(self.peer,'completed')

    async def test_cancelled_scan_joins_io_and_never_publishes_a_terminal_receipt(self):
        await self.begin();started=asyncio.Event();release=threading.Event();loop=asyncio.get_running_loop();original=Census.page
        def blocked(census):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(4):raise RuntimeError('controlled scan release timeout')
            return original(census)
        with self.file.runtime.gate.lease(exclusive=True) as lease,patch.object(Census,'page',blocked):
            task=asyncio.create_task(self.peer_complete.peer(self.relationship,self.proposal['binding'],lease))
            try:
                await asyncio.wait_for(started.wait(),2);task.cancel();await asyncio.sleep(.02);self.assertFalse(task.done())
                with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.file.runtime.gate.lease()
            finally:release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        await self.phase(self.peer,'retiring');await self.peer_done();await self.phase(self.peer,'completed')

    async def test_restart_replays_identical_receipt_without_scanning_new_external_material(self):
        await self.begin();peer=await self.peer_done();self.file.runtime.close();self.file.native.close()
        self.file.native=runtime_gate.acquire(self.file.data);self.file.runtime=Runtime(self.file.data,self.file.native)
        self.peer=State(self.peer.database,self.file.vault.read_bytes());await self.peer.initialize();self.file.store=self.peer
        self.file.authority=await self.file.capture();self.file.runtime.authority=self.file.authority;self.bind_peer()
        path=self.file.paths['media']/'new-external-file';path.write_bytes(b'never deleted by receipt replay')
        with patch.object(self.file.authority,'census',side_effect=AssertionError('repeated a terminal census')):
            self.assertEqual(await self.peer_done(),peer)
        self.assertEqual(path.read_bytes(),b'never deleted by receipt replay');await self.master_done(peer['receipt'])
        await self.phase(self.master,'completed')

    async def test_master_repeats_selected_member_ledger_after_peer_completion(self):
        await self.begin();peer=await self.peer_done()
        async with self.master.database.begin() as conn:await conn.execute(update(fs.storage_members).values(reserved_bytes=1))
        with self.assertRaisesRegex(RetirementError,'retirement_member_usage'):await self.master_done(peer['receipt'])
        async with self.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.retirements.c.phase)),'retiring')
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.reserved_bytes)),1)

    async def test_wrong_operation_and_missing_preparation_never_start_completion(self):
        self.proposal={'binding':{}}
        with self.assertRaisesRegex(RetirementError,'retirement_not_started'):await self.peer_done()
        await self.begin();changed={**self.proposal['binding'],'retirement_id':'f'*32}
        with self.file.runtime.gate.lease(exclusive=True) as lease:
            with self.assertRaisesRegex(RetirementError,'retirement_operation_conflict'):
                await self.peer_complete.peer(self.relationship,changed,lease)
        await self.phase(self.peer,'retiring')
