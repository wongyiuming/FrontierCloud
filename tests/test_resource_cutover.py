"""Real Master transactions after native publication and fresh Source proof."""
import asyncio
import hashlib
import os
import unittest
import uuid
from unittest.mock import patch
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sqlalchemy import delete,func,insert,select,text,update

from app.services import karaoke_schema as ks,media_crypto,media_crypto_schema as cs,recording_crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cutover_master import Cutover
from app.services.resource_migration_journal import Conflict
from app.services.resource_transfer_protocol import TransferError,target_request
from tests import test_resource_source_verify as fixtures


@unittest.skipUnless(os.name=='posix','cutover fixture requires native publication')
class CutoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.SourceVerificationTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f
        self.service=Cutover(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def asyncTearDown(self):await self.fixture.asyncTearDown()

    async def prepare(self):
        await self.fixture.prepare();await self.fixture.finish()
        self.lease,self.unit_id=self.fixture.lease,self.fixture.unit_id
        self.resources=self.fixture.fixture.resources;self.amount=sum(len(data) for _,_,data in self.resources)

    async def members(self):
        async with self.f.master.database.connect() as conn:
            return {row['member_id']:dict(row) for row in (await conn.execute(select(fs.storage_members))).mappings()}

    async def unchanged(self):await self.fixture.fixture.assert_uncommitted()

    async def test_atomic_owner_and_exact_master_quota_keep_live_business_and_crypto_facts(self):
        await self.prepare();logical,path,_=self.resources[0]
        stamp='2026-10-20 01:02:03';relative=path.relative_to(self.f.source.roots['media']).as_posix()
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.global_media).values(updated_at=999))
            await conn.execute(text('INSERT INTO media_visibility VALUES (:path,1,:stamp)'),dict(path=relative,stamp=stamp))
            await conn.execute(text('INSERT INTO media_playback_stats VALUES (:id,:path,1234,17,:stamp,:stamp)'),
                dict(id=logical,path=relative,stamp=stamp))
            await conn.execute(text('INSERT INTO media_lyric_links VALUES (:id,:path,:lyric,:lrc,:stamp,:stamp)'),
                dict(id=logical,path=relative,lyric='c'*64,lrc='lyrics/category/current.lrc',stamp=stamp))
            crypto_before=[dict(row) for row in (await conn.execute(select(cs.encryption))).mappings()]
            media_before={row['media_id']:dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()}
        old_members=await self.members();self.lease,result=await self.service.commit(self.lease,self.unit_id)
        self.assertTrue(result['cutover']);self.assertFalse(result['native_acknowledged']);self.assertFalse(result['source_files_removed'])
        self.assertEqual(result['commit']['cleanup_after']-result['commit']['committed_at'],3600)
        async with self.f.master.database.connect() as conn:
            rows={row['media_id']:dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()}
            self.assertEqual(set(rows),set(media_before))
            for key,row in rows.items():
                self.assertEqual(row,{**media_before[key],'storage_member_id':self.f.target.store.node['node_id']})
            self.assertEqual([dict(row) for row in (await conn.execute(select(cs.encryption))).mappings()],crypto_before)
            self.assertEqual(await conn.scalar(text('SELECT hidden FROM media_visibility')),1)
            self.assertEqual(tuple((await conn.execute(text('SELECT play_score,preference FROM media_playback_stats'))).one()),(1234,17))
            self.assertEqual(await conn.scalar(text('SELECT lyric_path FROM media_lyric_links')),'lyrics/category/current.lrc')
            self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),self.amount)
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),0)
            self.assertEqual(await conn.scalar(select(s.units.c.state)),'committed')
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),2)
        members=await self.members();source=self.f.source.store.node['node_id'];target=self.f.target.store.node['node_id']
        self.assertEqual(members[source]['used_bytes'],old_members[source]['used_bytes']-self.amount)
        self.assertEqual(members[target]['used_bytes'],old_members[target]['used_bytes']+self.amount)
        self.assertEqual(members[target]['reserved_bytes'],old_members[target]['reserved_bytes']-self.amount)
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),self.amount)
            self.assertEqual(await conn.scalar(select(s.units.c.state)),'published')
        for _,source_path,data in self.resources:
            self.assertEqual(source_path.read_bytes(),data)
            self.assertEqual((self.f.target.roots['media']/source_path.relative_to(self.f.source.roots['media'])).read_bytes(),data)

    async def test_stale_source_or_target_proof_never_changes_owner_or_capacity(self):
        await self.prepare();self.f.now+=46
        with self.assertRaisesRegex(TransferError,'proof_stale'):await self.service.commit(self.lease,self.unit_id)
        await self.unchanged()

    async def test_changed_master_catalog_or_descriptor_is_not_overwritten(self):
        await self.prepare()
        async with self.f.master.database.begin() as conn:await conn.execute(update(cs.encryption).values(descriptor_json=None))
        with self.assertRaisesRegex(TransferError,'catalog_changed'):await self.service.commit(self.lease,self.unit_id)
        await self.unchanged()
        async with self.f.master.database.connect() as conn:self.assertIsNone(await conn.scalar(select(cs.encryption.c.descriptor_json)))

    async def test_unexpected_master_sibling_blocks_partial_parent_cutover(self):
        await self.prepare()
        async with self.f.master.database.begin() as conn:
            row=dict((await conn.execute(select(fs.global_media).limit(1))).mappings().one())
            row.update(media_id='a'*64,object_id='b'*64,media_path='music/album/unplanned.mp3',path_locator='c'*64)
            await conn.execute(insert(fs.global_media).values(**row))
        with self.assertRaisesRegex(TransferError,'catalog_changed'):await self.service.commit(self.lease,self.unit_id)
        await self.unchanged()

    async def test_missing_reservation_or_scope_is_not_recreated_by_commit(self):
        await self.prepare()
        async with self.f.master.database.begin() as conn:await conn.execute(delete(s.capacity))
        with self.assertRaisesRegex(TransferError,'capacity_inconsistent'):await self.service.commit(self.lease,self.unit_id)
        async with self.f.master.database.connect() as conn:
            self.assertEqual(set((await conn.execute(select(fs.global_media.c.storage_member_id))).scalars()),{self.f.source.store.node['node_id']})
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.capacity)),0)

    async def test_final_audit_failure_rolls_back_all_owner_and_quota_updates(self):
        await self.prepare();before=await self.members();log=self.f.master.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_parent_committed':raise OSError('injected cutover audit failure')
            return await log(conn,action,actor,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.service.commit(self.lease,self.unit_id)
        self.assertEqual(await self.members(),before);await self.unchanged()
        self.lease,result=await self.service.commit(self.lease,self.unit_id);self.assertTrue(result['cutover'])

    async def test_unrelated_target_reservations_and_usage_are_preserved(self):
        await self.prepare();target=self.f.target.store.node['node_id']
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==target)
                .values(used_bytes=37,reserved_bytes=self.amount+23))
        self.lease,_=await self.service.commit(self.lease,self.unit_id)
        current=(await self.members())[target]
        self.assertEqual((current['used_bytes'],current['reserved_bytes']),(37+self.amount,23))
        await self.acknowledged()
        current=(await self.members())[target]
        self.assertEqual((current['used_bytes'],current['reserved_bytes']),(37+self.amount,23))

    async def test_parallel_old_lease_cannot_commit_twice(self):
        await self.prepare()
        results=await asyncio.gather(self.service.commit(self.lease,self.unit_id),self.service.commit(self.lease,self.unit_id),return_exceptions=True)
        self.assertEqual(sum(isinstance(item,BaseException) for item in results),1)
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),self.amount)

    async def test_lost_commit_reply_and_new_runner_return_same_receipt_without_second_charge(self):
        await self.prepare();original=self.lease
        self.lease,first=await self.service.commit(self.lease,self.unit_id);members=await self.members()
        self.f.now+=61
        self.lease=await self.service.transfer.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        self.lease,again=await self.service.commit(self.lease,self.unit_id)
        self.assertEqual(first,again);self.assertEqual(await self.members(),members)
        with self.assertRaises(Conflict):await self.service.commit(original,self.unit_id)

    async def test_missing_master_parent_scope_is_not_recreated_by_commit(self):
        await self.prepare()
        async with self.f.master.database.begin() as conn:await conn.execute(delete(s.scopes)
            .where(s.scopes.c.member_id==self.f.target.store.node['node_id']))
        with self.assertRaisesRegex(TransferError,'scope_missing'):await self.service.commit(self.lease,self.unit_id)
        await self.unchanged()
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),1)

    async def acknowledged(self):
        for _ in range(3):
            result=await self.service.acknowledge_one(self.lease,self.unit_id)
            if result['native_acknowledged']:return result
        self.fail('native acknowledgements did not converge')

    async def test_native_ack_consumes_target_capacity_once_and_keeps_source_and_all_fences(self):
        await self.prepare();self.lease,_=await self.service.commit(self.lease,self.unit_id)
        self.assertTrue((await self.acknowledged())['native_acknowledged'])
        for peer in (self.f.source,self.f.target):
            async with peer.database.connect() as conn:
                self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),0)
                self.assertEqual(await conn.scalar(select(s.units.c.state)),'committed')
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),1)
                member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==peer.store.node['node_id']))).mappings().one()
                if peer==self.f.target:self.assertEqual((member['used_bytes'],member['reserved_bytes']),(self.amount,0))
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)
        self.assertTrue((await self.acknowledged())['native_acknowledged'])

    async def test_lost_native_reply_and_new_runner_do_not_charge_target_twice(self):
        await self.prepare();self.lease,_=await self.service.commit(self.lease,self.unit_id)
        async def lost(path,value,result):
            if path.endswith('/commit-ack'):raise OSError('injected lost native acknowledgement')
        self.fixture.fixture.after=lost
        with self.assertRaises(OSError):await self.service.acknowledge_one(self.lease,self.unit_id)
        self.fixture.fixture.after=None;self.f.now+=61
        self.lease=await self.service.transfer.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        await self.acknowledged()
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes).where(
                fs.storage_members.c.member_id==self.f.target.store.node['node_id'])),self.amount)

    async def test_native_audit_failure_rolls_back_capacity_then_retry_succeeds(self):
        await self.prepare();self.lease,_=await self.service.commit(self.lease,self.unit_id)
        log=self.f.target.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_native_parent_committed':raise OSError('injected native audit failure')
            return await log(conn,action,actor,**kwargs)
        with patch.object(self.f.target.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.service.acknowledge_one(self.lease,self.unit_id)
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),self.amount)
            self.assertEqual(await conn.scalar(select(s.units.c.state)),'published')
        await self.acknowledged()

    async def test_master_ack_audit_failure_retries_already_committed_peer(self):
        await self.prepare();self.lease,_=await self.service.commit(self.lease,self.unit_id)
        log=self.f.master.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_parent_commit_acknowledged':raise OSError('injected master acknowledgement audit failure')
            return await log(conn,action,actor,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.service.acknowledge_one(self.lease,self.unit_id)
        await self.acknowledged()
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),0)

    async def test_boolean_native_progress_is_refused_and_valid_retry_recovers(self):
        await self.prepare();self.lease,_=await self.service.commit(self.lease,self.unit_id)
        async def corrupt(path,value,result):
            if path.endswith('/commit-ack'):result['committed_bytes']=True
        self.fixture.fixture.after=corrupt
        with self.assertRaisesRegex(TransferError,'native_reply_invalid'):await self.service.acknowledge_one(self.lease,self.unit_id)
        self.fixture.fixture.after=None;await self.acknowledged()

    async def test_old_source_copy_grant_cannot_reopen_a_verified_parent(self):
        await self.prepare();grant=self.fixture.fixture.fixture.grants[-1]
        request=target_request(self.f.target.store.unseal(self.f.target.store.node['private_key']),grant,offset=0,length=32,now=self.f.now)
        with self.assertRaisesRegex(TransferError,'unit_verified'):await self.f.source.transfers.source(request)
        self.lease,_=await self.service.commit(self.lease,self.unit_id);await self.acknowledged()

    async def prepare_recordings(self):
        user='b'*32;descriptor=media_crypto.Metadata.new(3)
        encrypted=AESGCM(bytes(32)).encrypt(descriptor.chunk_nonce(0),b'[]\n',descriptor.chunk_aad(0))
        snapshot=dict(encryption=descriptor.to_dict(),ciphertext=media_crypto._encode64(encrypted))
        self.resources=[]
        for index,footer in enumerate((None,dict(version=1,title='footer',lyrics=[]),
                dict(version=1,title='opaque',lyrics=[],encrypted_lyrics=snapshot))):
            recording=str(index+1)*32;data=b'RIFF original opaque recording bytes'
            if footer is not None:
                raw=p.canonical(footer);data+=raw+len(raw).to_bytes(8,'big')+recording_crypto.TRAILER_MAGIC
            path=self.f.source.roots['recordings']/self.f.source.relationship/user/(recording+'.bin')
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);self.resources.append((recording,path,data))
            async with self.f.master.database.begin() as conn:
                await conn.execute(insert(ks.recordings).values(recording_id=recording,user_id=user,
                    storage_member_id=self.f.source.store.node['node_id'],filename=f'original-{index}.webm',content_type='audio/webm',
                    size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),state='ready',title='original',lyrics=[],created_at=1,updated_at=1))
        self.amount=sum(len(data) for _,_,data in self.resources)
        async with self.f.master.database.begin() as conn:await conn.execute(update(fs.storage_members)
            .where(fs.storage_members.c.member_id==self.f.source.store.node['node_id']).values(used_bytes=self.amount))
        publication=self.fixture.fixture;transfer=publication.fixture
        transfer.plan=await self.f.plan();self.assertEqual(transfer.plan['state'],'ready',transfer.plan)
        await transfer.fixture.admission.prepare(transfer.plan['plan_id'],transfer.plan['review_sha256'],'admin')
        await transfer.queue()
        for recording,_,_ in self.resources:
            transfer.logical=recording
            for _ in range(12):
                if (await transfer.step())['state']=='verified':break
            else:self.fail('recording copy did not converge')
        self.unit_id=transfer.grants[-1]['payload']['object']['unit_id']
        self.lease=await publication.service.enter(transfer.lease);self.lease=await publication.service.enter(self.lease)
        publication.lease=self.lease;publication.unit_id=self.unit_id;await publication.finish()
        return publication

    async def test_recording_parent_rebind_preserves_opaque_footer_and_current_master_business(self):
        publication=await self.prepare_recordings()
        with (patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster')),
                patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted'))):
            for _ in range(24):
                if (await publication.service.source_step(self.lease,self.unit_id))['source_verified']:break
            else:self.fail('recording Source proof did not converge')
            async with self.f.master.database.begin() as conn:
                await conn.execute(update(ks.recordings).values(title='current title',lyrics=[dict(time=1,text='current lyric')],updated_at=999))
                before={row['recording_id']:dict(row) for row in (await conn.execute(select(ks.recordings))).mappings()}
            self.lease,_=await self.service.commit(self.lease,self.unit_id);await self.acknowledged()
        async with self.f.master.database.connect() as conn:
            after={row['recording_id']:dict(row) for row in (await conn.execute(select(ks.recordings))).mappings()}
        self.assertEqual(after,{key:{**row,'storage_member_id':self.f.target.store.node['node_id']} for key,row in before.items()})
        for recording,source,data in self.resources:
            target=self.f.target.roots['recordings']/self.f.target.relationship/source.parent.name/(recording+'.bin')
            self.assertEqual(target.read_bytes(),data);self.assertEqual(source.read_bytes(),data)
        for peer in (self.f.source,self.f.target):
            async with peer.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(ks.recordings)),0)
