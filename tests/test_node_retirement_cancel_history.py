"""Mixed terminal history is read-only; valid outer signatures do not hide damage."""
import copy
import os
import unittest
from unittest.mock import patch

from sqlalchemy import insert,select,update

from app.services import media_crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_master_history import MasterHistory
from app.services.node_retirement_history import NativeHistory
from app.services.resource_cancel_terminal_receipts import COMPLETION
from app.services.resource_observation import digest
from tests import test_resource_cancel_finalize_master as fixtures
from tests import test_resource_peer_reservations as reservations


@unittest.skipUnless(os.name=='posix','mixed cancellation needs native Linux fixtures')
class CancelledMasterHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        original=reservations.PeerReservationTests.asyncSetUp
        async def single_clock(fixture):
            await original(fixture)
            for peer in (fixture.f.source,fixture.f.target):
                peer.reservations.wall=lambda:fixture.f.now
                peer.observer.wall=lambda:fixture.f.now
        self.case=fixtures.MasterCancellationFinalizationTests('runTest')
        with patch.object(reservations.PeerReservationTests,'asyncSetUp',single_clock):await self.case.asyncSetUp()
        await self.case.reach('cancelled');self.f=self.case.f

    async def asyncTearDown(self):await self.case.asyncTearDown()

    async def snapshot(self):
        async with self.f.master.database.connect() as conn:
            return {table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()]
                for table in (*s.metadata.tables.values(),fs.global_media,fs.storage_members,fs.audit)}

    async def verify(self):
        async with self.f.master.database.connect() as conn:
            return await MasterHistory(self.f.master.store,self.f.now).verify(conn,self.f.master.store.node,self.f.source.store.node['node_id'])

    async def unchanged_refusal(self):
        before=await self.snapshot()
        with self.assertRaises(RetirementError):await self.verify()
        self.assertEqual(await self.snapshot(),before);await self.case.case.retained()

    async def replace_completion(self,change):
        async with self.f.master.database.begin() as conn:
            task=(await conn.execute(select(s.tasks))).mappings().one();plan=task['plan'];value=copy.deepcopy(plan[COMPLETION]['payload'])
            change(value)
            envelope=p.sign(self.f.master.store.unseal(self.f.master.store.node['private_key']),value)
            await conn.execute(update(s.tasks).values(plan={**plan,COMPLETION:envelope,COMPLETION+'_sha256':digest(envelope)}))

    async def test_mixed_master_history_is_readonly_and_has_no_network_or_decryption(self):
        before=await self.snapshot()
        with (patch.object(self.f.transport,'request',side_effect=AssertionError('History issued a request')),
                patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('History decrypted media'))):
            self.assertEqual(await self.verify(),dict(master_tasks=1))
            async with self.f.master.database.connect() as conn:
                self.assertEqual(await MasterHistory(self.f.master.store,self.f.now).verify(conn,self.f.master.store.node,
                    self.f.target.store.node['node_id']),dict(master_tasks=1))
        self.assertEqual(await self.snapshot(),before);await self.case.case.retained()

    async def test_valid_master_signature_cannot_hide_damaged_native_source_receipt(self):
        await self.replace_completion(lambda value:value['source_record']['receipt']['payload'].update(native_used_after=0))
        await self.unchanged_refusal()

    async def test_boolean_counter_with_valid_master_signature_refuses(self):
        await self.replace_completion(lambda value:value.update(version=True))
        await self.unchanged_refusal()

    async def test_damaged_cleanup_history_refuses_and_preserves_original_rows(self):
        async with self.f.master.database.begin() as conn:
            unit=(await conn.execute(select(s.units).where(s.units.c.state=='cleaned'))).mappings().one();publication=unit['publication']
            await conn.execute(update(s.units).where(s.units.c.unit_id==unit['unit_id']).values(publication={**publication,
                'source_cleanup_reply':{**publication['source_cleanup_reply'],'deleted_count':True}}))
        await self.unchanged_refusal()

    async def test_foreign_object_is_not_hidden_by_valid_terminal_record(self):
        async with self.f.master.database.begin() as conn:
            original=dict((await conn.execute(select(s.objects).limit(1))).mappings().one())
            await conn.execute(insert(s.objects).values(**{**original,'logical_id':'f'*64}))
        await self.unchanged_refusal()

    async def test_missing_cancelled_object_is_not_accepted_as_a_legacy_abort(self):
        from sqlalchemy import delete
        async with self.f.master.database.begin() as conn:
            logical=await conn.scalar(select(s.objects.c.logical_id).where(s.objects.c.state=='cancelled').limit(1))
            await conn.execute(delete(s.objects).where(s.objects.c.logical_id==logical))
        await self.unchanged_refusal()

    async def test_nonterminal_state_still_refuses_a_valid_terminal_record(self):
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.tasks).values(state='cancelling'))
        await self.unchanged_refusal()


@unittest.skipUnless(os.name=='posix','native cancellation history needs Linux')
class CancelledNativeHistoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=CancelledMasterHistoryTests.asyncSetUp
    asyncTearDown=CancelledMasterHistoryTests.asyncTearDown

    async def snapshot(self,peer):
        async with peer.database.connect() as conn:
            return {table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()]
                for table in (*s.metadata.tables.values(),fs.storage_members,fs.audit)}

    async def verify_peer(self,peer):
        async with peer.database.connect() as conn:return await NativeHistory(peer.store,self.f.now).verify(conn,peer.store.node)

    async def refused(self,peer):
        before=await self.snapshot(peer)
        with self.assertRaises(RetirementError):await self.verify_peer(peer)
        self.assertEqual(await self.snapshot(peer),before);await self.case.case.retained()

    async def test_mixed_native_source_and_target_history_is_readonly(self):
        for peer in (self.f.source,self.f.target):
            before=await self.snapshot(peer)
            with (patch.object(p,'sign',side_effect=AssertionError('History issued a capability')),
                    patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('History decrypted media')),
                    patch('app.services.resource_observation.os.pread',side_effect=AssertionError('History read payload'))):
                self.assertEqual(await self.verify_peer(peer),dict(native_tasks=1))
            self.assertEqual(await self.snapshot(peer),before)
        await self.case.case.retained()

    async def test_changed_target_unlink_intent_cannot_hide_behind_a_valid_release(self):
        peer=self.f.target
        async with peer.database.begin() as conn:
            entry=(await conn.execute(select(s.storage_journal).where(s.storage_journal.c.state=='aborted').limit(1))).mappings().one()
            await conn.execute(update(s.storage_journal).where(s.storage_journal.c.logical_id==entry['logical_id']).values(cleanup_version='f'*64))
        await self.refused(peer)

    async def test_damaged_source_release_receipt_is_preserved_and_refused(self):
        peer=self.f.source
        async with peer.database.begin() as conn:
            header=(await conn.execute(select(s.peer_reservations))).mappings().one();payload=copy.deepcopy(header['payload'])
            payload['cancel_release']['receipt']['payload']['native_used_after']=0
            await conn.execute(update(s.peer_reservations).values(payload=payload))
        await self.refused(peer)

    async def test_missing_source_binding_is_not_a_complete_terminal_history(self):
        from sqlalchemy import delete
        peer=self.f.source
        async with peer.database.begin() as conn:
            logical=await conn.scalar(select(s.peer_bindings.c.logical_id).limit(1))
            await conn.execute(delete(s.peer_bindings).where(s.peer_bindings.c.logical_id==logical))
        await self.refused(peer)

    async def test_source_cancelled_unit_cannot_have_an_unexplained_cleanup_journal(self):
        peer=self.f.source
        async with peer.database.begin() as conn:
            unit=await conn.scalar(select(s.units.c.unit_id).where(s.units.c.state=='cancelled'))
            logical=await conn.scalar(select(s.peer_bindings.c.logical_id).where(s.peer_bindings.c.unit_id==unit).limit(1))
            entry=dict((await conn.execute(select(s.storage_journal).limit(1))).mappings().one())
            await conn.execute(insert(s.storage_journal).values(**{**entry,'logical_id':logical,'unit_id':unit}))
        await self.refused(peer)

    async def test_owned_capacity_still_blocks_valid_native_terminal_records(self):
        peer=self.f.target
        async with peer.database.begin() as conn:
            task_id=await conn.scalar(select(s.peer_reservations.c.task_id))
            await conn.execute(insert(s.capacity).values(task_id=task_id,member_id=peer.store.node['node_id'],reserved_bytes=0,updated_at=int(self.f.now)))
        await self.refused(peer)


if __name__=='__main__':unittest.main()
