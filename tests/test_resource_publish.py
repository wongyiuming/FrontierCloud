"""Real private/public inodes and transactions, before global ownership cutover."""
import copy
import json
import os
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import func,select,text,update

from app.services import media_crypto,resource_migration_schema as s,resource_publish_files as files
from app.services.federation import protocol as p,schema as fs
from app.services.resource_observation import digest
from app.services.resource_publish_master import Publication
from app.services.resource_publish_peers import PublishPeers
from app.services.resource_source_verify import SourceVerification
from app.services.resource_commit_peers import CommitPeers
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_transfer as fixtures


@unittest.skipUnless(os.name=='posix','native publication requires Linux descriptors')
class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.ResourceTransferTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f
        self.original_control=self.f.transport.request;self.f.transport.request=self.control
        self.service=Publication(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.before=None;self.after=None

    async def asyncTearDown(self):await self.fixture.asyncTearDown()

    async def control(self,origin,path,**kwargs):
        verifying=path.endswith('/verify-source')
        committing=path.endswith('/commit-ack')
        if '/publish-' not in path and not verifying and not committing:return await self.original_control(origin,path,**kwargs)
        peer=self.f.source if verifying or committing and origin==self.f.source.store.node['endpoint'] else self.f.target
        self.assertEqual(origin,peer.store.node['endpoint'])
        self.assertEqual((kwargs['credential'],kwargs['relation']),(peer.credential,peer.relationship))
        async with peer.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==peer.relationship))).mappings().one())
        if self.before:await self.before(path,kwargs['value'])
        action=(CommitPeers(peer.transfers).acknowledge if committing else SourceVerification(peer.transfers).advance if verifying else
            PublishPeers(peer.transfers).describe if path.endswith('metadata') else PublishPeers(peer.transfers).advance)
        result=await action(peer.store.node,relation,**kwargs['value'])
        if self.after:await self.after(path,kwargs['value'],result)
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)

    async def prepare(self,*,pair=False,empty=False):
        self.resources=[]
        self.resources.append(await self.f.media(b'ID3 original bytes'*300))
        if pair:self.resources.append(await self.f.media(b'encrypted original'*400,path='music/album/private.mp3',encrypted=True))
        if empty:(self.f.target.roots['media']/'music/album').mkdir(parents=True)
        self.fixture.plan=await self.f.plan();self.assertEqual(self.fixture.plan['state'],'ready',self.fixture.plan)
        await self.fixture.fixture.admission.prepare(self.fixture.plan['plan_id'],self.fixture.plan['review_sha256'],'admin')
        await self.fixture.queue()
        for logical,_,_ in self.resources:
            self.fixture.logical=logical
            while (await self.fixture.step())['state']!='verified':pass
        self.copy_grant=copy.deepcopy(self.fixture.grants[-1])
        self.unit_id=self.copy_grant['payload']['object']['unit_id']
        self.lease=await self.service.enter(self.fixture.lease)
        self.lease=await self.service.enter(self.lease)
        self.private=self.f.target.roots['media']/'.frontiercloud-migration'/self.lease.task_id/self.unit_id
        self.target=self.f.target.roots['media']/'music/album'

    async def step(self):return await self.service.target_step(self.lease,self.unit_id)

    async def finish(self):
        for _ in range(80):
            result=await self.step()
            if result['target_published']:return result
        self.fail('target publication did not converge')

    async def reach(self,phase):
        for _ in range(80):
            result=await self.step()
            if result['phase']==phase:return result
        self.fail('publication phase not reached: '+phase)

    async def assert_uncommitted(self):
        async with self.f.master.database.connect() as conn:
            owners=set((await conn.execute(select(fs.global_media.c.storage_member_id))).scalars())
            self.assertEqual(owners,{self.f.source.store.node['node_id']})
            self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),0)
        self.assertEqual(await self.fixture.fixture.quota(),(sum(len(data) for _,_,data in self.resources),)*2)
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)

    async def test_whole_plain_cipher_parent_publishes_without_copying_payload_or_switching_master(self):
        await self.prepare(pair=True,empty=True)
        old={path.name:os.stat(self.private/(logical+'.part')).st_ino for logical,path,_ in self.resources}
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster'))):
            result=await self.finish()
        self.assertFalse(result['cutover']);self.assertFalse(result['capacity_settled'])
        for _,path,data in self.resources:
            published=self.target/path.name
            self.assertEqual(published.read_bytes(),data)
            self.assertEqual(published.stat().st_ino,old[path.name])
            self.assertEqual(published.stat().st_nlink,1)
        self.assertFalse(self.private.exists())
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM media_objects')),2)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(fs.global_media)),0)
            self.assertEqual(await conn.scalar(select(s.units.c.state)),'published')
        await self.assert_uncommitted()

    async def test_incomplete_copy_cannot_freeze_the_receiver_for_publication(self):
        await self.fixture.prepare(large=True);await self.fixture.queue();await self.fixture.step()
        self.lease=self.fixture.lease
        with self.assertRaisesRegex(TransferError,'copies_incomplete'):await self.service.enter(self.lease)
        self.assertGreater((await self.fixture.step())['received_bytes'],0)

    async def test_changed_staged_inode_or_bytes_are_refused_before_assembly(self):
        await self.prepare();await self.reach('assembling')
        path=self.private/(self.resources[0][0]+'.part');path.write_bytes(b'changed private bytes')
        with self.assertRaisesRegex(TransferError,'file_changed'):await self.step()
        self.assertFalse(self.target.exists());self.assertTrue(path.exists())
        await self.assert_uncommitted()

    async def test_unknown_assembly_file_is_preserved_with_reservations(self):
        await self.prepare();await self.reach('scan_private')
        unknown=self.private/'ready/unknown.mp3';unknown.write_bytes(b'keep unknown')
        with self.assertRaisesRegex(TransferError,'unknown_entry'):await self.step()
        self.assertEqual(unknown.read_bytes(),b'keep unknown')
        self.assertFalse(self.target.exists());await self.assert_uncommitted()

    async def test_unknown_private_material_blocks_directory_move(self):
        await self.prepare();await self.reach('move')
        unknown=self.private/'unknown.bin';unknown.write_bytes(b'keep unknown')
        with self.assertRaisesRegex(TransferError,'unknown_entry'):await self.step()
        self.assertEqual(unknown.read_bytes(),b'keep unknown');self.assertFalse(self.target.exists())
        await self.assert_uncommitted()

    async def test_late_target_directory_is_never_overwritten(self):
        await self.prepare();await self.reach('move')
        self.target.mkdir();unknown=self.target/'outside.mp3';unknown.write_bytes(b'keep target')
        with self.assertRaisesRegex(TransferError,'target_changed'):await self.step()
        self.assertEqual(unknown.read_bytes(),b'keep target');await self.assert_uncommitted()

    async def test_link_then_unlink_failure_resumes_same_inode_without_payload_copy(self):
        await self.prepare();await self.reach('assembling')
        unlink=files.os.unlink
        def fail(name,*args,**kwargs):
            if str(name).endswith('.part'):raise OSError('injected unlink failure')
            return unlink(name,*args,**kwargs)
        with patch.object(files.os,'unlink',side_effect=fail):
            with self.assertRaises(OSError):await self.step()
        part=self.private/(self.resources[0][0]+'.part');assembled=self.private/'ready/song.mp3'
        self.assertEqual(part.stat().st_ino,assembled.stat().st_ino);self.assertEqual(part.stat().st_nlink,2)
        await self.finish();self.assertEqual((self.target/'song.mp3').stat().st_nlink,1)
        await self.assert_uncommitted()

    async def test_directory_move_then_lost_sql_ack_reconciles_existing_target(self):
        await self.prepare();await self.reach('move')
        save=PublishPeers._save
        async def failed(service,grant,unit,**values):
            if values.get('step')=='register':raise OSError('injected lost move acknowledgement')
            return await save(service,grant,unit,**values)
        with patch.object(PublishPeers,'_save',failed):
            with self.assertRaises(OSError):await self.step()
        self.assertEqual((self.target/'song.mp3').read_bytes(),self.resources[0][2])
        await self.finish();await self.assert_uncommitted()

    async def test_new_receiver_resumes_registration_and_preserves_original_encryption(self):
        await self.prepare(pair=True);await self.reach('register')
        replacement=TransferPeers(self.f.target.store,root_provider=lambda:self.f.target.roots,traffic=self.f.traffic,
            wall=lambda:self.f.now,client=self.fixture.source_client)
        self.f.target.transfers=replacement;self.fixture.services.append(replacement)
        await self.finish()
        await self.assert_uncommitted()
        async with self.f.target.database.connect() as conn:
            encrypted=await media_crypto.encryption_for_id(conn,self.copy_grant['payload']['object']['source_object'])
        # Copy order is the explicit resources order, with the encrypted file last.
        self.assertIsNotNone(encrypted)

    async def test_lost_master_reply_retries_target_receipt_with_fresh_file_check(self):
        await self.prepare();await self.reach('scan_public')
        original=self.f.master.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_target_publication_step':raise OSError('injected master audit failure')
            return await original(conn,action,actor,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.step()
        await self.finish();await self.assert_uncommitted()

    async def test_old_copy_grant_cannot_reopen_a_publishing_unit(self):
        await self.prepare();await self.reach('assembling')
        peer=self.f.target
        async with peer.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==peer.relationship))).mappings().one())
        # The frozen parent rejects copying; another admitted parent can run
        # after this one commits, even though task publication has started.
        with self.assertRaisesRegex(TransferError,'migration_transfer_unit_changed'):
            await peer.transfers.copy(peer.store.node,relation,uuid.uuid4().hex,self.copy_grant)
        await self.finish();await self.assert_uncommitted()

    async def test_equal_counts_do_not_replace_the_source_bound_unit_roster(self):
        await self.prepare();await self.reach('roster')
        async with self.f.target.database.begin() as conn:
            row=(await conn.execute(select(s.peer_bindings))).mappings().one()
            value=json.loads(row['binding_json']);value['object']['source_version']='0'*64
            await conn.execute(update(s.peer_bindings).values(binding_json=p.canonical(value).decode(),binding_sha256=digest(value)))
            await conn.execute(update(s.storage_journal).values(source_version='0'*64))
        with self.assertRaisesRegex(TransferError,'roster_changed'):await self.finish()
        self.assertFalse(self.target.exists());await self.assert_uncommitted()

    async def test_signed_target_reply_after_grant_expiry_is_not_accepted(self):
        await self.prepare()
        async def delay(path,value,result):self.f.now+=46
        self.after=delay
        with self.assertRaisesRegex(TransferError,'target_reply_invalid'):await self.step()
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.units.c.publication)),{})
        await self.assert_uncommitted()

    async def test_published_target_reply_must_report_the_published_phase(self):
        await self.prepare();await self.reach('scan_public')
        async def corrupt(path,value,result):
            if result['state']=='published':result['phase']='assembling'
        self.after=corrupt
        with self.assertRaisesRegex(TransferError,'target_reply_invalid'):await self.finish()
        self.after=None;await self.finish();await self.assert_uncommitted()
