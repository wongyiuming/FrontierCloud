"""Real three-store publication and Master cancellation permission boundaries.

The cancelling phase is a controlled fixture, not a claim that the public
post-publication cancel operation has already been integrated.
"""
import os
import unittest
import uuid
from unittest.mock import AsyncMock,patch

from sqlalchemy import select,text,update

from app.core import db
from app.services import media_crypto,media_crypto_schema as cs,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_authority import CancellationAuthority,verify
from app.services.resource_migration_journal import Conflict
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cutover as fixtures


@unittest.skipUnless(os.name=='posix','cancellation authority fixture uses native Linux publication')
class CancellationAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.CutoverTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f
        self.authority=CancellationAuthority(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def asyncTearDown(self):await self.fixture.asyncTearDown()

    async def prepare(self,*,committed=False):
        await self.fixture.prepare();self.unit=self.fixture.unit_id;self.lease=self.fixture.lease
        if committed:self.lease,_=await self.fixture.service.commit(self.lease,self.unit)

    async def cancelling(self):
        self.old_lease=self.lease
        async with self.f.master.database.begin() as conn:
            row=(await conn.execute(select(s.tasks))).mappings().one()
            await conn.execute(update(s.tasks).values(state='cancelling',revision=row['revision']+1))
            await conn.execute(update(s.runners).values(expires_at=0))
        self.lease=await self.authority.transfer.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)

    async def snapshot(self):
        async with self.f.master.database.connect() as conn:
            return {name:list((await conn.execute(text(f'SELECT * FROM `{name}`'))).tuples())
                for name in sorted((await db._table_names(conn))-{fs.audit.name,s.runners.name})}

    def master_key(self):return p.public_key(self.f.master.store.unseal(self.f.master.store.node['private_key']))

    async def originals(self):
        for _,path,data in self.fixture.resources:
            self.assertEqual(path.read_bytes(),data)
            self.assertEqual((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).read_bytes(),data)

    async def test_signed_permission_preserves_every_business_crypto_scope_and_quota_row(self):
        await self.prepare();await self.cancelling();before=await self.snapshot();calls=len(self.f.calls)
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('media decryption')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('media premaster'))):
            envelope,grant,unit=await self.authority.issue(self.lease,self.unit)
        result=verify(envelope,self.master_key(),grant,unit,self.f.now)
        self.assertTrue(result['source_owns_unit']);self.assertFalse(result['ownership_rollback'])
        self.assertEqual(await self.snapshot(),before);self.assertEqual(len(self.f.calls),calls)
        await self.originals()

    async def test_master_commit_blocks_shadow_permission_even_when_native_acknowledgement_was_lost(self):
        await self.prepare(committed=True)
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.units.c.state)),'published')
            self.assertEqual(await conn.scalar(select(s.units.c.committed_at)),0)
        await self.cancelling();before=await self.snapshot()
        with self.assertRaisesRegex(TransferError,'master_commit_is_final'):
            await self.authority.issue(self.lease,self.unit)
        self.assertEqual(await self.snapshot(),before);await self.originals()

    async def test_cancelling_request_is_required_before_issuing_a_permission(self):
        await self.prepare();before=await self.snapshot()
        with self.assertRaisesRegex(TransferError,'cancel_request_required'):
            await self.authority.issue(self.lease,self.unit)
        self.assertEqual(await self.snapshot(),before);await self.originals()

    async def test_changed_master_owner_cannot_be_approved_from_the_old_plan(self):
        await self.prepare();await self.cancelling()
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.global_media).where(fs.global_media.c.media_id==self.fixture.resources[0][0])
                .values(storage_member_id=self.f.target.store.node['node_id']))
        before=await self.snapshot()
        with self.assertRaisesRegex(TransferError,'catalog_changed'):await self.authority.issue(self.lease,self.unit)
        self.assertEqual(await self.snapshot(),before);await self.originals()

    async def test_changed_encryption_descriptor_prevents_shadow_permission(self):
        await self.prepare();await self.cancelling()
        async with self.f.master.database.begin() as conn:await conn.execute(update(cs.encryption).values(descriptor_json=None))
        before=await self.snapshot()
        with self.assertRaisesRegex(TransferError,'catalog_changed'):await self.authority.issue(self.lease,self.unit)
        self.assertEqual(await self.snapshot(),before);await self.originals()

    async def test_prior_or_expired_runner_cannot_obtain_cancellation_authority(self):
        await self.prepare();await self.cancelling()
        with self.assertRaises(Conflict):await self.authority.issue(self.old_lease,self.unit)
        self.f.now+=61
        with self.assertRaises(Conflict):await self.authority.issue(self.lease,self.unit)
        await self.originals()

    async def test_expired_or_wrongly_bound_or_malformed_signed_permissions_are_refused(self):
        await self.prepare();await self.cancelling();envelope,grant,unit=await self.authority.issue(self.lease,self.unit)
        private=self.f.master.store.unseal(self.f.master.store.node['private_key'])
        changes=({'version':True},{'epoch':True},{'revision':0},{'object_count':True},
            {'source_owns_unit':1},{'ownership_rollback':0},{'target_id':'f'*32},
            {'expires_at':envelope['payload']['expires_at']+1})
        for values in changes:
            bad=p.sign(private,{**envelope['payload'],**values})
            with self.subTest(values=values),self.assertRaisesRegex(TransferError,'authorization_invalid'):
                verify(bad,self.master_key(),grant,unit,self.f.now)
        with self.assertRaisesRegex(TransferError,'authorization_expired'):
            verify(envelope,self.master_key(),grant,unit,envelope['payload']['expires_at'])
        with self.assertRaisesRegex(TransferError,'authorization_invalid'):
            verify(envelope,self.master_key(),grant,unit,envelope['payload']['created_at']-6)
        foreign=p.sign(self.f.source.store.unseal(self.f.source.store.node['private_key']),envelope['payload'])
        with self.assertRaisesRegex(TransferError,'authorization_invalid'):verify(foreign,self.master_key(),grant,unit,self.f.now)
        await self.originals()

    async def test_missing_owned_capacity_keeps_all_files_and_fences(self):
        await self.prepare();await self.cancelling()
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.capacity).values(reserved_bytes=0))
        before=await self.snapshot()
        with self.assertRaisesRegex(TransferError,'capacity_inconsistent'):await self.authority.issue(self.lease,self.unit)
        self.assertEqual(await self.snapshot(),before);await self.originals()

    async def test_changed_relation_cannot_be_approved_using_a_historical_plan(self):
        await self.prepare();await self.cancelling()
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.relationships).where(fs.relationships.c.peer_id==self.f.target.store.node['node_id'])
                .values(peer_endpoint='https://changed.4399.com'))
        before=await self.snapshot()
        with self.assertRaisesRegex(TransferError,'members_changed'):await self.authority.issue(self.lease,self.unit)
        self.assertEqual(await self.snapshot(),before);await self.originals()

    async def test_audit_failure_returns_no_permission_and_changes_no_domain_row(self):
        await self.prepare();await self.cancelling();before=await self.snapshot()
        with patch.object(self.f.master.store,'log',new=AsyncMock(side_effect=OSError('controlled audit failure'))):
            with self.assertRaises(OSError):await self.authority.issue(self.lease,self.unit)
        self.assertEqual(await self.snapshot(),before);await self.originals()
