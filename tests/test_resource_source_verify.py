"""Fresh Source unit proof, without rehash/decryption or ownership mutation."""
import json
import os
import unittest
from unittest.mock import patch

from sqlalchemy import select,text

from app.services import media_crypto,resource_migration_schema as s
from app.services.business_traffic import HIGH,Monitor
from app.services.federation import protocol as p,schema as fs
from app.services.federation.transport import ControlFailure
from app.services.resource_control_errors import response as error_response
from app.services.resource_cutover_master import Cutover
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_publish as fixtures


@unittest.skipUnless(os.name=='posix','source verification requires Linux descriptors')
class SourceVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.PublicationTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f

    async def asyncTearDown(self):await self.fixture.asyncTearDown()

    async def prepare(self):
        await self.fixture.prepare(pair=True);await self.fixture.finish()
        self.service=self.fixture.service;self.lease=self.fixture.lease;self.unit_id=self.fixture.unit_id

    async def step(self):return await self.service.source_step(self.lease,self.unit_id)

    async def finish(self):
        for _ in range(24):
            result=await self.step()
            if result['source_verified']:return result
        self.fail('source verification did not converge')

    async def test_plain_cipher_roster_and_live_metadata_verified_without_payload_hashing_or_keys(self):
        await self.prepare()
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('reread payload'))):
            result=await self.finish()
        self.assertTrue(result['source_verified']);self.assertFalse(result['cutover'])
        self.assertIsNone(self.f.source.transfers.source_verification)
        async with self.f.source.database.connect() as conn:
            row=(await conn.execute(select(s.units))).mappings().one()
            proof=row['publication']['source_verification']
            self.assertEqual((row['state'],proof['object_count'],proof['total_bytes']),
                ('verified',2,sum(len(data) for _,_,data in self.fixture.resources)))
        await self.fixture.assert_uncommitted()

    async def test_source_only_traffic_pause_keeps_active_reader_and_refreshes_expired_target_proof(self):
        await self.prepare()
        source=self.f.source.transfers;previous_traffic=source.traffic
        traffic=Monitor(monotonic=lambda:self.f.now,wall=lambda:self.f.now)
        source.traffic=traffic
        self.f.now+=30
        self.assertTrue(traffic.snapshot()['can_run'])
        self.assertIsNot(traffic,self.service.transfer.journal.traffic)
        self.assertIsNot(traffic,self.f.target.transfers.traffic)
        original=self.f.transport.request;signed_errors=[]

        async def signed_control(origin,path,**kwargs):
            try:return await original(origin,path,**kwargs)
            except TransferError as exc:
                if not path.endswith('/verify-source'):raise
                self.assertEqual(origin,self.f.source.store.node['endpoint'])
                async with self.f.source.database.connect() as conn:
                    relation=dict((await conn.execute(select(fs.relationships).where(
                        fs.relationships.c.relationship_id==self.f.source.relationship))).mappings().one())
                envelope=json.loads(error_response(self.f.source.store,path=path,raw=p.canonical(kwargs['value']),
                    relation=relation,exc=exc,prefix='migration_verify').body)
                signed_errors.append(envelope)
                raise ControlFailure(409,envelope) from exc

        async def journal_state():
            async with self.f.master.database.connect() as conn:
                task=dict((await conn.execute(select(s.tasks))).mappings().one())
                unit=dict((await conn.execute(select(s.units))).mappings().one())
            async with self.f.source.database.connect() as conn:
                source_unit=dict((await conn.execute(select(s.units))).mappings().one())
            return task,unit,source_unit

        traffic.entered()
        try:
            # An independent Source reader can burst while Master/Target stay quiet.
            traffic.record(egress=HIGH)
            before=await journal_state()
            self.assertTrue(self.service.transfer.journal.traffic.snapshot()['can_run'])
            self.assertTrue(self.f.target.transfers.traffic.snapshot()['can_run'])
            with patch.object(self.f.transport,'request',signed_control):
                with self.assertRaisesRegex(TransferError,'^migration_transfer_waiting_low_traffic$'):
                    await self.step()
                self.assertEqual(await journal_state(),before)
                await self.fixture.assert_uncommitted()
                self.assertIsNone(source.source_verification)
                # Advance a unit-test clock, observing both the drain and quiet interval.
                self.f.now+=5
                self.assertEqual(traffic.snapshot()['resume_after_seconds'],30)
                self.f.now+=29
                with self.assertRaisesRegex(TransferError,'^migration_transfer_waiting_low_traffic$'):
                    await self.step()
                self.assertEqual(await journal_state(),before)
                self.f.now+=1
                view=traffic.snapshot()
                self.assertTrue(view['can_run']);self.assertFalse(view['readers_clear'])
                self.assertEqual(view['active_business_requests'],1)
                self.assertTrue((await self.finish())['source_verified'])
            source_key=p.public_key(self.f.source.store.unseal(self.f.source.store.node['private_key']))
            self.assertEqual(len(signed_errors),2)
            for envelope in signed_errors:
                self.assertEqual(p.verify(source_key,envelope)['code'],'migration_transfer_waiting_low_traffic')
            await self.fixture.assert_uncommitted()
            coordinator=Cutover(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)
            with self.assertRaisesRegex(TransferError,'^migration_commit_target_proof_stale$'):
                await coordinator.commit(self.lease,self.unit_id)
            await self.fixture.assert_uncommitted()
            # Refresh through the real Target publication service, never edit a proof deadline.
            self.assertTrue((await self.fixture.finish())['target_published'])
            self.lease,result=await coordinator.commit(self.lease,self.unit_id)
            self.assertTrue(result['cutover']);self.assertFalse(result['source_files_removed'])
            self.assertEqual(result['commit']['cleanup_after']-result['commit']['committed_at'],3600)
            async with self.f.master.database.connect() as conn:
                owners=set((await conn.execute(select(fs.global_media.c.storage_member_id))).scalars())
                self.assertEqual(owners,{self.f.target.store.node['node_id']})
                self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),
                    sum(len(data) for _,_,data in self.fixture.resources))
            self.assertEqual(traffic.snapshot()['active_business_requests'],1)
            for _,path,data in self.fixture.resources:
                self.assertEqual(path.read_bytes(),data)
                self.assertEqual((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).read_bytes(),data)
        finally:
            traffic.exited();source.traffic=previous_traffic

    async def test_changed_source_bytes_even_with_restored_mtime_refuse_cutover_proof(self):
        await self.prepare();path=self.fixture.resources[0][1];before=path.stat()
        path.write_bytes(b'x'*before.st_size);os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        with self.assertRaisesRegex(TransferError,'source_changed'):await self.finish()
        self.assertEqual(path.read_bytes(),b'x'*before.st_size)
        self.assertFalse(self.f.source.transfers.source_lock.locked())
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),0)

    async def test_changed_native_metadata_refuses_proof_without_rewriting_metadata(self):
        await self.prepare()
        async with self.f.source.database.begin() as conn:
            await conn.execute(text('UPDATE media_objects SET updated_at=:stamp'),dict(stamp='2026-10-20 00:00:00'))
        with self.assertRaisesRegex(TransferError,'source_changed'):await self.finish()
        self.assertIsNone(self.f.source.transfers.source_verification)
        await self.fixture.assert_uncommitted()

    async def test_unknown_direct_child_remains_and_no_source_proof_is_persisted(self):
        await self.prepare();unknown=self.fixture.resources[0][1].parent/'unknown.mp3';unknown.write_bytes(b'keep')
        with self.assertRaisesRegex(TransferError,'unknown_entry'):await self.finish()
        self.assertEqual(unknown.read_bytes(),b'keep');self.assertIsNone(self.f.source.transfers.source_verification)
        async with self.f.source.database.connect() as conn:
            self.assertNotIn('source_verification',await conn.scalar(select(s.units.c.publication)))
        await self.fixture.assert_uncommitted()

    async def test_directory_change_between_bounded_steps_rejects_old_iterator(self):
        await self.prepare();self.assertEqual((await self.step())['phase'],'objects')
        unknown=self.fixture.resources[0][1].parent/'new.mp3';unknown.write_bytes(b'keep new')
        with self.assertRaisesRegex(TransferError,'directory_changed'):await self.step()
        self.assertEqual(unknown.read_bytes(),b'keep new');await self.fixture.assert_uncommitted()

    async def test_new_source_service_restarts_roster_and_closes_old_directory(self):
        await self.prepare();await self.step();old=self.f.source.transfers;scan=old.source_verification
        await old.close();self.assertIsNone(scan.descriptor)
        replacement=TransferPeers(self.f.source.store,root_provider=lambda:self.f.source.roots,
            traffic=self.f.traffic,wall=lambda:self.f.now)
        self.f.source.transfers=replacement;self.fixture.fixture.services.append(replacement)
        await self.finish();await self.fixture.assert_uncommitted()

    async def test_lost_master_audit_reply_repeats_fresh_source_verification(self):
        await self.prepare();await self.step();await self.step()
        original=self.f.master.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_source_verification_step':raise OSError('injected master audit failure')
            return await original(conn,action,actor,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.step()
        self.assertIsNone(self.f.source.transfers.source_verification)
        self.assertEqual((await self.step())['phase'],'objects')
        await self.finish();await self.fixture.assert_uncommitted()

    async def test_signed_reply_received_after_capability_expiry_is_not_accepted(self):
        await self.prepare()
        async def delay(path,value,result):
            if path.endswith('/verify-source'):self.f.now+=46
        self.fixture.after=delay
        with self.assertRaisesRegex(TransferError,'source_reply_invalid'):await self.step()
        async with self.f.master.database.connect() as conn:
            self.assertNotIn('source_reply',await conn.scalar(select(s.units.c.publication)))
        await self.fixture.assert_uncommitted()

    async def test_boolean_progress_in_a_signed_reply_is_refused_and_retry_recovers(self):
        await self.prepare()
        async def corrupt(path,value,result):
            if path.endswith('/verify-source'):result['processed_objects']=True
        self.fixture.after=corrupt
        with self.assertRaisesRegex(TransferError,'source_reply_invalid'):await self.step()
        self.fixture.after=None;await self.finish();await self.fixture.assert_uncommitted()
