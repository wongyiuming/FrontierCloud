"""Mixed native Target release preserves data, other work and restart evidence."""
import os
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import func,insert,select,update

from app.services import media_crypto,resource_migration_schema as s,resource_transfer_files as staging
from app.services.federation import schema as fs
from app.services.resource_cancel_target_release import TargetRelease,KEY
from app.services.resource_cancel_target_receipts import released,source_record
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_source_release as fixtures


@unittest.skipUnless(os.name=='posix','native target release requires Linux staging directories')
class TargetReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.SourceReleaseTests.asyncSetUp(self)
        result=await fixtures.SourceReleaseTests.apply(self)
        self.witness=dict(version=1,agreement=result['agreement'],source_census=result['source_census'],receipt=result['receipt'])
        self.target=TargetRelease(self.f.target.transfers)
        async with self.f.target.database.connect() as conn:self.target_relation=dict((await conn.execute(select(fs.relationships))).mappings().one())

    asyncTearDown=fixtures.SourceReleaseTests.asyncTearDown
    new_catalog=fixtures.SourceReleaseTests.new_catalog
    finish=fixtures.SourceReleaseTests.finish
    step=fixtures.SourceReleaseTests.step
    retained=fixtures.SourceReleaseTests.retained

    async def grant(self):
        await self.catalog.t.journal.renew(self.lease,ttl=60)
        async with self.f.master.database.begin() as conn:task=await self.catalog.t.journal._fenced(conn,self.lease)
        return self.catalog.t._grant(task,await self.catalog.t.relations(task),self.lease.epoch,self.lease.runner_id,'finalize')

    async def target_step(self,*,witness=None):
        return await self.target.advance(self.f.target.store.node,self.target_relation,uuid.uuid4().hex,await self.grant(),witness or self.witness)

    async def reach(self,phase):
        for _ in range(40):
            result=await self.target_step()
            if result['phase']==phase:return result
        self.fail('Target release did not reach '+phase)

    async def active_target(self):
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.peer_reservations.c.state)),'active')
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),1)
        await self.retained()

    async def test_mixed_release_rechecks_committed_target_without_payload_reads_or_usage_change(self):
        async with self.f.target.database.connect() as conn:before=[dict(row) for row in (await conn.execute(select(fs.storage_members))).mappings()]
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted media')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read payload'))):
            result=await self.reach('prepared')
        # The final helper may read only the explicitly bound 32-byte probe.
        result=await self.target_step();self.assertEqual(result['phase'],'released')
        grant=(await self.grant())['payload']
        source=source_record(self.witness,grant,(2,3,sum(len(data) for _,_,data in self.case.resources)),self.target_relation['peer_key'],self.f.now)
        receipt=released(result['receipt'],grant,source,self.witness,
            (await self.header())['payload']['transfer']['staging_roots'],result['preparation'],self.f.now)
        self.assertEqual(receipt['native_used_before'],receipt['native_used_after']);self.assertFalse(receipt['task_completed'])
        async with self.f.target.database.connect() as conn:
            self.assertEqual([dict(row) for row in (await conn.execute(select(fs.storage_members))).mappings()],before)
            self.assertEqual(await conn.scalar(select(s.peer_reservations.c.state)),'released')
            for table in (s.scopes,s.capacity):self.assertEqual(await conn.scalar(select(func.count()).select_from(table)),0)
        await self.retained()

    async def header(self):
        async with self.f.target.database.connect() as conn:return dict((await conn.execute(select(s.peer_reservations))).mappings().one())

    async def test_audit_failure_after_empty_root_removal_recovers_from_signed_preparation(self):
        await self.reach('prepared');original=self.f.target.store.log
        async def fail_final(conn,action,*args,**kwargs):
            if action=='migration_native_cancel_target_released':raise RuntimeError('fixture final audit failure')
            return await original(conn,action,*args,**kwargs)
        with patch.object(self.f.target.store,'log',side_effect=fail_final):
            with self.assertRaisesRegex(RuntimeError,'final audit failure'):await self.target_step()
        await self.active_target()
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.runners).values(expires_at=0))
        self.lease=await self.catalog.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        self.target=TargetRelease(self.f.target.transfers)
        self.assertEqual((await self.target_step())['phase'],'released');await self.retained()

    async def test_unknown_private_entry_is_preserved_and_blocks_release(self):
        await self.reach('prepared')
        path=self.f.target.roots['media']/staging.DIRECTORY/self.lease.task_id/'fixture-owner-data'
        path.write_bytes(b'Preserve unknown private bytes')
        with self.assertRaisesRegex(TransferError,'unknown_stage'):await self.target_step()
        self.assertEqual(path.read_bytes(),b'Preserve unknown private bytes');await self.active_target()
        saved=self.f.root/'fixture-owner-preserved-private';path.rename(saved)
        self.assertEqual((await self.target_step())['phase'],'released');self.assertEqual(saved.read_bytes(),b'Preserve unknown private bytes')

    async def test_unrelated_capacity_playback_and_new_public_bytes_survive(self):
        path=self.f.target.roots['media']/(self.f.migration_retained_parent+'/fixture-later.mp3');path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'Preserve later public bytes')
        async with self.f.target.database.begin() as conn:
            await conn.execute(insert(s.capacity).values(task_id='f'*32,member_id=self.f.target.store.node['node_id'],reserved_bytes=17,updated_at=int(self.f.now)))
            await conn.execute(update(fs.storage_members).values(reserved_bytes=17,used_bytes=fs.storage_members.c.used_bytes+path.stat().st_size))
            await conn.execute(insert(s.read_leases).values(lease_id='a'*32,member_id=self.f.target.store.node['node_id'],logical_id=self.case.resources[0][0],
                object_id='b'*64,source_version='c'*64,task_id='',purpose='playback',expires_at=int(self.f.now)+600))
        result=await self.reach('released');self.assertEqual(path.read_bytes(),b'Preserve later public bytes')
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),17)
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.reserved_bytes)),17)
            self.assertEqual(await conn.scalar(select(s.read_leases.c.lease_id)),'a'*32)
        self.assertEqual(result['receipt']['payload']['native_reserved_after'],17);await self.retained()

    async def test_tampered_source_receipt_never_authorizes_target_release(self):
        changed={**self.witness,'receipt':{**self.witness['receipt'],'payload':{**self.witness['receipt']['payload'],'native_used_after':0}}}
        with self.assertRaisesRegex(TransferError,'receipt_invalid'):await self.target_step(witness=changed)
        await self.active_target()

    async def test_unsigned_cursor_change_cannot_skip_unit_validation(self):
        await self.reach('prepared')
        async with self.f.target.database.begin() as conn:
            header=(await conn.execute(select(s.peer_reservations))).mappings().one();payload=header['payload'];control=payload['transfer']
            progress={**control[KEY],'after':'f'*64}
            await conn.execute(update(s.peer_reservations).values(payload={**payload,'transfer':{**control,KEY:progress}}))
        with self.assertRaisesRegex(TransferError,'progress_changed'):await self.target_step()
        await self.active_target()

    async def test_damaged_committed_public_file_refuses_and_preserves_other_files(self):
        _,source,data=self.case.resources[0];path=self.f.target.roots['media']/source.relative_to(self.f.source.roots['media'])
        path.write_bytes(b'x'*len(data))
        with self.assertRaisesRegex(TransferError,'changed'):await self.reach('released')
        self.assertEqual(path.read_bytes(),b'x'*len(data))
        async with self.f.target.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.peer_reservations.c.state)),'active')

    async def test_replay_after_new_epoch_preserves_later_target_usage(self):
        result=await self.reach('released')
        async with self.f.target.database.begin() as conn:await conn.execute(update(fs.storage_members).values(used_bytes=fs.storage_members.c.used_bytes+1234))
        self.f.now+=100
        async with self.f.master.database.begin() as conn:await conn.execute(update(fs.relationships).values(last_heartbeat=int(self.f.now)))
        self.lease=await self.catalog.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        self.target=TargetRelease(self.f.target.transfers)
        with patch.object(self.f.target.store,'log',side_effect=AssertionError('Repeated target release')):second=await self.target_step()
        self.assertEqual(second['receipt'],result['receipt'])
        async with self.f.target.database.connect() as conn:self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),result['receipt']['payload']['native_used_after']+1234)
        await self.retained()


if __name__=='__main__':unittest.main()
