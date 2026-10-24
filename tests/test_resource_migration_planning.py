"""Three real SQLite stores and native files with actual signed service clients.

The in-process transport exercises service contracts, not HTTPS transport proof.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.engine import URL

from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_crypto as crypto, media_manager, karaoke_schema as ks
from app.services import resource_migration_schema as s
from app.services import media_crypto_schema as cs
from app.services import resource_inventory as inventory_module
from app.services.business_traffic import Monitor
from app.services.federation import protocol as p, schema as fs
from app.services.federation.state import State
from app.services.resource_inventory import Inventory
from app.services.resource_migration_planning import Planner
from app.services.resource_observation import ContentObserver, digest
from app.services.resource_plan_store import PlanError
from app.services.resource_registry import NativeRegistry


@unittest.skipUnless(os.name == 'posix','migration planning uses native Linux openat')
class MigrationPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = time.time()
        self.traffic = Monitor(monotonic=lambda:1000,wall=lambda:1000)
        self.traffic._quiet_since = 900
        self.nodes = {}
        for name, role in (('master','Master'),('source','Follower'),('target','Follower')):
            root = self.root/name
            root.mkdir()
            database = create_sqlite_engine(URL.create('sqlite+aiosqlite',database=str(root/'state.db')))
            with patch.object(db,'engine',database),patch.object(db,'SECRET_DIR',root/'secrets'):
                await db.init_db()
            vault_key=Fernet.generate_key()
            store = State(database,vault_key)
            await store.initialize()
            async with database.begin() as conn:
                await conn.execute(update(fs.identity).values(role=role,endpoint=f'https://{name}.4399.com'))
            store.node.update(role=role,endpoint=f'https://{name}.4399.com')
            roots = {scope:root/'data'/scope for scope in ('media','recordings')}
            roots['data'] = root/'data'
            for path in roots.values():
                path.mkdir(parents=True,exist_ok=True)
            (roots['data']/'identity-note').write_bytes(name.encode())
            inventory = Inventory(root_provider=lambda roots=roots:roots,traffic=self.traffic)
            observer = ContentObserver(store,root_provider=lambda roots=roots:roots,traffic=self.traffic)
            self.nodes[name] = SimpleNamespace(name=name,store=store,database=database,roots=roots,vault_key=vault_key,
                inventory=inventory,observer=observer,registry=NativeRegistry(store,observer,inventory))
        self.master,self.source,self.target = (self.nodes[name] for name in ('master','source','target'))
        for index, peer in enumerate((self.source,self.target)):
            relationship, credential = str(index+7)*32,p.encode(os.urandom(32))
            common = dict(relationship_id=relationship,mode='Direct' if index == 0 else 'Relay',
                state='active',status='online',last_heartbeat=int(self.now),rtt_ms=1,failures=0,recoveries=0,
                peer_version=p.APP_VERSION,protocol=p.PROTOCOL_VERSION,summary={},created_at=1)
            peer.relationship,peer.credential = relationship,credential
            for local,remote,direction in ((self.master,peer,'downstream'),(peer,self.master,'upstream')):
                async with local.database.begin() as conn:
                    await conn.execute(insert(fs.relationships).values(**common,peer_id=remote.store.node['node_id'],
                        peer_endpoint=remote.store.node['endpoint'],peer_key=p.public_key(remote.store.unseal(remote.store.node['private_key'])),
                        direction=direction,credential=local.store.seal(credential)))
            async with self.master.database.begin() as conn:
                await conn.execute(insert(fs.storage_members).values(member_id=peer.store.node['node_id'],
                    relationship_id=relationship,member_kind='Follower',transport=common['mode'],storage_enabled=1,
                    allocated_bytes=8*1024**3,used_bytes=0,reserved_bytes=0,physical_free_bytes=16*1024**3,
                    health='online',writable=1,updated_at=int(self.now)))
        self.calls = []
        self.hook = None
        self.transport = SimpleNamespace(request=self.request,identity=self.identity)
        self.planner = Planner(self.master.store,self.transport,traffic=self.traffic,wall=lambda:self.now)

    async def asyncTearDown(self):
        await self.planner.stop()
        for peer in self.nodes.values():
            await peer.inventory.stop()
            await peer.observer.stop()
            await peer.database.dispose()
        self.temp.cleanup()

    async def identity(self,origin,*,expected_id,expected_key,role):
        peer = next(peer for peer in self.nodes.values() if peer.store.node['endpoint'] == origin)
        value = p.verify(expected_key,peer.store.identity(uuid.uuid4().hex))
        self.assertEqual((value['node_id'],value['role']),(expected_id,role))
        return value

    async def request(self,origin,path,**kwargs):
        peer = next(peer for peer in self.nodes.values() if peer.store.node['endpoint'] == origin)
        self.assertEqual(kwargs['credential'],peer.credential)
        self.assertEqual(kwargs['relation'],peer.relationship)
        value = kwargs['value']
        self.calls.append((peer.name,path,dict(value)))
        if self.hook is not None:
            await self.hook(peer,path,value)
        async with peer.database.connect() as conn:
            relationship = dict((await conn.execute(select(fs.relationships).where(
                fs.relationships.c.relationship_id == peer.relationship))).mappings().one())
        action = path.rsplit('/',1)[-1]
        operation = (peer.registry.page if '/registry/' in path else
                     getattr(peer.observer if '/observe/' in path else peer.inventory,action))
        with patch.object(media_manager,'MEDIA_ROOT',peer.roots['media']):
            result = await operation(peer.store.node,relationship,**value)
        if result is None:
            result = dict(closed=True,session_id=value['session_id'],node_id=peer.store.node['node_id'])
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)

    async def media(self,data=b'ID3-original',*,path='music/album/song.mp3',encrypted=False,peer=None):
        peer = self.source if peer is None else peer
        object_id = uuid.uuid4().hex*2
        logical_id = p.resource_id(peer.store.node['node_id'],object_id)
        descriptor = None
        if encrypted:
            descriptor = crypto.Metadata.new(len(data))
            cipher=AESGCM(bytes(32))
            data=b''.join(cipher.encrypt(descriptor.chunk_nonce(index),data[offset:offset+crypto.CHUNK_SIZE],
                descriptor.chunk_aad(index)) for index,offset in enumerate(range(0,len(data),crypto.CHUNK_SIZE)))
        native = peer.roots['media']/path
        native.parent.mkdir(parents=True,exist_ok=True)
        native.write_bytes(data)
        async with peer.database.begin() as conn:
            await conn.execute(text('INSERT INTO media_objects VALUES (:id,:kind,:path,:locator,:created,:updated)'),
                dict(id=object_id,kind='audio',path=path,locator=hashlib.sha256(path.encode()).hexdigest(),
                     created='2026-01-01 00:00:00',updated='2026-01-01 00:00:00'))
            if descriptor is not None:
                await crypto.put_encryption(conn,object_id,descriptor)
        async with self.master.database.begin() as conn:
            await conn.execute(insert(fs.global_media).values(media_id=logical_id,storage_member_id=peer.store.node['node_id'],
                object_id=object_id,media_path=path,path_locator=hashlib.sha256(path.encode()).hexdigest(),object_kind='audio',
                size_bytes=len(data),etag=hashlib.sha256(data).hexdigest(),state='active',created_at=1,updated_at=1))
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id == peer.store.node['node_id'])
                .values(used_bytes=fs.storage_members.c.used_bytes+len(data)))
            if descriptor is not None:
                await crypto.put_encryption(conn,logical_id,descriptor)
        return logical_id,native,data

    async def plan(self,*,request_id=None):
        plan_id = uuid.uuid4().hex if request_id is None else request_id
        await self.planner.start(plan_id,self.source.store.node['node_id'],self.target.store.node['node_id'],'test-admin')
        await asyncio.wait_for(asyncio.shield(self.planner.task),20)
        return await self.planner.status(plan_id)

    async def codes(self,result):
        return {value['code'] for value in result['report']['blockers']}

    async def legacy_ciphertext(self):
        resource=await self.media(b'original encrypted upload'*400,encrypted=True)
        async with self.source.database.begin() as conn:await conn.execute(delete(cs.encryption))
        return resource

    async def test_master_only_descriptor_binds_source_ciphertext_without_backfill_or_decryption(self):
        resource=await self.legacy_ciphertext()
        with (patch.object(crypto,'open_premaster',side_effect=AssertionError('premaster opened')),
                patch.object(crypto.Manager,'file_key',side_effect=AssertionError('decrypted'))):
            result=await self.plan()
        self.assertEqual(result['state'],'ready',result)
        async with self.master.database.connect() as conn:
            encoded=await conn.scalar(select(s.plan_bindings.c.object_json).where(s.plan_bindings.c.logical_id==resource[0]))
        snapshot=json.loads(encoded)
        self.assertIsNotNone(snapshot['encryption'])
        self.assertEqual(snapshot['source_encryption_sha256'],digest(None))
        self.assertEqual(snapshot['source_proof']['result']['encryption_sha256'],digest(None))
        async with self.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(cs.encryption)),0)
        self.assertEqual(resource[1].read_bytes(),resource[2])

    async def test_master_only_descriptor_requires_original_ciphertext_checksum(self):
        await self.legacy_ciphertext()
        async with self.master.database.begin() as conn:await conn.execute(update(fs.global_media).values(etag='legacy-weak-tag'))
        result=await self.plan();self.assertEqual(result['state'],'blocked',result)
        self.assertIn('migration_source_registration_mismatch',await self.codes(result))

    async def test_master_only_descriptor_refuses_same_length_changed_ciphertext(self):
        resource=await self.legacy_ciphertext();resource[1].write_bytes(b'x'*len(resource[2]))
        result=await self.plan();self.assertEqual(result['state'],'blocked',result)
        self.assertIn('migration_source_hash_mismatch',await self.codes(result))

    async def test_different_nonempty_source_descriptor_is_never_legacy_compatibility(self):
        resource=await self.media(b'ciphertext fixture'*400,encrypted=True)
        async with self.source.database.begin() as conn:
            identifier=await conn.scalar(text('SELECT media_id FROM media_objects'))
            await conn.execute(delete(cs.encryption))
            await crypto.put_encryption(conn,identifier,crypto.Metadata.new(len(b'ciphertext fixture'*400)),restore=True)
        result=await self.plan();self.assertEqual(result['state'],'blocked',result)
        self.assertIn('migration_source_registration_mismatch',await self.codes(result))
        self.assertEqual(resource[1].read_bytes(),resource[2])

    async def test_ready_plan_keeps_plain_ciphertext_identity_and_all_bytes_without_admission(self):
        first = await self.media()
        second = await self.media(b'private original'*6000,path='music/album/encrypted.mp3',encrypted=True)
        with (patch.object(crypto,'open_premaster',side_effect=AssertionError('premaster opened')),
              patch.object(crypto.Manager,'file_key',side_effect=AssertionError('media decrypted'))):
            result = await self.plan()
        self.assertEqual(result['state'],'ready',result)
        self.assertEqual((result['object_count'],result['unit_count']),(2,1))
        self.assertEqual(result['report']['observed_objects'],2)
        self.assertTrue(result['report']['blockers_complete'])
        self.assertEqual(result['report']['residual_count'],2)
        self.assertFalse(result['report']['capacity']['writable_verified'])
        self.assertEqual({item['logical_id'] for item in result['objects']},{first[0],second[0]})
        for _,path,data in (first,second):
            self.assertEqual(path.read_bytes(),data)
        async with self.master.database.connect() as conn:
            for table in (s.tasks,s.capacity,s.drains,s.scopes):
                self.assertEqual(await conn.scalar(select(func.count()).select_from(table)),0)
        again = await self.planner.start(result['plan_id'],self.source.store.node['node_id'],self.target.store.node['node_id'],'retry')
        self.assertEqual(again['review_sha256'],result['review_sha256'])
        self.assertEqual(len([value for value in self.calls if value[1].endswith('/inventory/start')]),12)

    async def test_recording_physical_parent_rebind_is_read_only_and_keeps_business_snapshot(self):
        identifier,user = 'a'*32,'b'*32
        data = b'RIFF-original-recording'
        source_path = self.source.roots['recordings']/self.source.relationship/user/(identifier+'.bin')
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(data)
        async with self.master.database.begin() as conn:
            await conn.execute(insert(ks.recordings).values(recording_id=identifier,user_id=user,
                storage_member_id=self.source.store.node['node_id'],filename='original.webm',content_type='audio/webm',
                size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),state='ready',title='original',
                lyrics=[{'time':0,'text':'original lyric'}],created_at=1,updated_at=1))
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id == self.source.store.node['node_id']).values(used_bytes=len(data)))
        result = await self.plan()
        self.assertEqual(result['state'],'ready',result)
        self.assertEqual(result['objects'][0]['target_path'],self.target.relationship+'/'+user+'/'+identifier+'.bin')
        self.assertEqual(source_path.read_bytes(),data)
        async with self.master.database.connect() as conn:
            row = (await conn.execute(select(ks.recordings))).mappings().one()
        self.assertEqual((row['filename'],row['title'],row['lyrics']),('original.webm','original',[{'time':0,'text':'original lyric'}]))

    async def test_every_target_descendant_conflict_is_counted_with_bounded_samples(self):
        await self.media()
        folder = self.target.roots['media']/'music/album/nested/deeper'
        folder.mkdir(parents=True)
        for index in range(80):
            (folder/str(index)).write_bytes(b'preserve')
        result = await self.plan()
        self.assertEqual(result['state'],'blocked')
        self.assertGreaterEqual(result['report']['blocker_count'],82)
        self.assertEqual(len(result['report']['blockers']),64)
        self.assertTrue(result['report']['inventory_complete'])
        self.assertFalse(result['report']['blockers_complete'])
        self.assertNotIn('path',result['report']['blockers'][0])
        self.assertTrue(all(path.read_bytes() == b'preserve' for path in folder.iterdir()))
        self.assertFalse(any('/observe/' in path for _,path,_ in self.calls))

    async def test_target_file_introduced_during_hashing_blocks_the_final_rescan(self):
        await self.media()
        created = False
        async def hook(peer,path,value):
            nonlocal created
            if '/observe/start' in path and not created:
                destination = self.target.roots['media']/'music/album/new.bin'
                destination.parent.mkdir(parents=True)
                destination.write_bytes(b'do not erase')
                created = True
        self.hook = hook
        result = await self.plan()
        self.assertEqual(result['state'],'blocked',result)
        self.assertIn('migration_target_parent_occupied',await self.codes(result))

    async def test_source_rewrite_after_completed_hash_is_detected_by_final_stat(self):
        _,native,data = await self.media()
        rewritten = False
        async def hook(peer,path,value):
            nonlocal rewritten
            if '/observe/close' in path and not rewritten:
                native.write_bytes(b'x'*len(data))
                rewritten = True
        self.hook = hook
        result = await self.plan()
        self.assertEqual(result['state'],'blocked',result)
        self.assertIn('migration_source_changed_after_hash',await self.codes(result))
        self.assertEqual(native.read_bytes(),b'x'*len(data))

    async def test_registered_missing_source_and_unregistered_source_are_both_reported(self):
        _,native,_ = await self.media()
        native.unlink()
        (native.parent/'unknown.mp3').write_bytes(b'keep')
        result = await self.plan()
        self.assertEqual(result['state'],'blocked',result)
        self.assertTrue({'migration_source_file_missing','migration_source_unmanaged_or_unsupported'} <= await self.codes(result))

    async def test_capacity_recheck_uses_latest_native_free_without_creating_reservation(self):
        await self.media()
        scans = 0
        original = self.target.inventory.start
        walker = inventory_module.Walker
        def low_space(path):
            value = walker(path)
            value.free_bytes = 1024
            return value
        async def tight(*args,**kwargs):
            nonlocal scans
            scans += 1
            if scans > 3:
                with patch.object(inventory_module,'Walker',side_effect=low_space):
                    return await original(*args,**kwargs)
            return await original(*args,**kwargs)
        with patch.object(self.target.inventory,'start',side_effect=tight):
            result = await self.plan()
        self.assertEqual(result['state'],'blocked',result)
        self.assertIn('migration_target_capacity_insufficient',await self.codes(result),result)
        self.assertEqual(result['report']['capacity']['native_free_bytes'],1024)

    async def test_native_duplicate_survives_conflict_rollback(self):
        await self.media()
        facts,_,_ = await self.planner.spool.facts(self.source.store.node['node_id'],self.target.store.node['node_id'])
        plan_id = uuid.uuid4().hex
        await self.planner.spool.create(plan_id,facts,self.planner.epoch,'test')
        entry = dict(path='music/unknown.mp3',kind='file',links=1)
        for code in ('unmanaged_or_unsupported','native_duplicate'):
            with self.assertRaisesRegex(PlanError,code):
                await self.planner.spool.native_entry(plan_id,self.source.store.node['node_id'],self.source.store.node['node_id'],
                    self.source.relationship,'media',entry)

    async def test_restart_and_expiry_reject_ready_preview_and_reap_expired_rows(self):
        await self.media()
        result = await self.plan()
        self.assertEqual(result['state'],'ready',result)
        changed = Planner(self.master.store,self.transport,traffic=self.traffic,wall=lambda:self.now)
        with self.assertRaisesRegex(PlanError,'expired_or_restarted'):
            await changed.status(result['plan_id'])
        self.now += 301
        with self.assertRaisesRegex(PlanError,'expired_or_restarted'):
            await self.planner.status(result['plan_id'])
        for _ in range(3):
            await self.planner.spool.reap_previews()
        async with self.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.plans)),0)

    async def test_reaper_preserves_admitted_and_task_referenced_rows(self):
        facts,_,_ = await self.planner.spool.facts(self.source.store.node['node_id'],self.target.store.node['node_id'])
        admitted, referenced = 'a'*32,'b'*32
        for identifier in (admitted,referenced):
            await self.planner.spool.create(identifier,facts,self.planner.epoch,'test')
        async with self.master.database.begin() as conn:
            await conn.execute(update(s.plans).values(expires_at=1))
            await conn.execute(update(s.plans).where(s.plans.c.plan_id == admitted).values(state='admitted'))
            await conn.execute(insert(s.tasks).values(task_id=referenced,master_id=facts['master_id'],
                source_id=facts['source_id'],target_id=facts['target_id'],source_relationship=self.source.relationship,
                target_relationship=self.target.relationship,plan_sha256='f'*64,plan={},state='queued',
                object_count=0,total_bytes=0,created_at=1,updated_at=1))
        await self.planner.spool.reap_previews()
        async with self.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.plans)),2)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.tasks)),1)

    async def test_busy_and_cancel_while_waiting_for_traffic_never_install_write_fences(self):
        _,native,data = await self.media()
        self.traffic._quiet_since = None
        plan_id = uuid.uuid4().hex
        source,target = self.source.store.node['node_id'],self.target.store.node['node_id']
        await self.planner.start(plan_id,source,target,'test')
        with self.assertRaisesRegex(PlanError,'planner_busy'):
            await self.planner.start(uuid.uuid4().hex,source,target,'test')
        await asyncio.sleep(.01)
        await self.planner.stop()
        result = await self.planner.status(plan_id)
        self.assertEqual(result['state'],'blocked')
        self.assertIn('migration_observation_interrupted',await self.codes(result))
        self.assertEqual(native.read_bytes(),data)
        async with self.master.database.connect() as conn:
            for table in (s.tasks,s.capacity,s.drains,s.scopes):
                self.assertEqual(await conn.scalar(select(func.count()).select_from(table)),0)

    async def test_peer_restart_after_hash_never_produces_a_ready_plan(self):
        await self.media()
        async def hook(peer,path,value):
            if path.endswith('/observe/close'):
                self.target.inventory.incarnation = uuid.uuid4().hex
        self.hook = hook
        result = await self.plan()
        self.assertEqual(result['state'],'blocked',result)
        self.assertIn('migration_peer_restarted',await self.codes(result))

    async def test_status_paths_are_readable_but_pages_stay_below_wire_budget(self):
        facts,_,_ = await self.planner.spool.facts(self.source.store.node['node_id'],self.target.store.node['node_id'])
        plan_id = uuid.uuid4().hex
        await self.planner.spool.create(plan_id,facts,self.planner.epoch,'test')
        for index in range(16):
            identifier = f'{index+1:064x}'
            path = 'music/'+'漢'*200+'/'+'字'*200+f'/{index}.mp3'
            snapshot = dict(master={},encryption=None,physical_path=path,target_path=path,
                parent_path=path.rsplit('/',1)[0],target_parent=path.rsplit('/',1)[0],source_object=identifier)
            await self.planner.spool.add(plan_id,dict(logical_id=identifier,resource_kind='media',snapshot=snapshot,
                unit_id=digest(snapshot['parent_path']),expected_bytes=3),facts['source_id'],facts['target_id'])
        identifiers,after = [],''
        for _ in range(5):
            result = await self.planner.status(plan_id,after=after)
            self.assertLessEqual(len(p.canonical(result)),48*1024)
            self.assertIn('漢',result['objects'][0]['physical_path'])
            identifiers.extend(value['logical_id'] for value in result['objects'])
            after = result['next']
            if result['complete']:
                break
        self.assertEqual(len(identifiers),16)
        self.assertEqual(len(set(identifiers)),16)
