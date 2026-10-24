"""Release agreements require both current authorities; signing releases nothing."""
import os
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete,select,update

from app.services import media_crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_agreement import FORMAT,IDENTITIES,verify
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_catalog as fixtures


class AgreementProtocolTests(unittest.TestCase):
    def setUp(self):
        self.master,self.source,self.target=p.new_key(),p.new_key(),p.new_key();self.now=1000;self.counts=(2,3,30)
        ids=('master_id','source_id','target_id','source_relationship','target_relationship','task_id','runner_id')
        self.grant={name:f'{index:032x}' for index,name in enumerate(ids,1)}
        self.grant.update({name:f'{index:064x}' for index,name in enumerate(
            ('plan_sha256','units_sha256','source_binding','target_binding'),20)})
        self.grant.update(source_key=p.public_key(self.source),target_key=p.public_key(self.target),
            epoch=3,purpose='finalize',created_at=self.now,expires_at=self.now+45)
        self.proof=dict(format='frontiercloud-python-source-cancel-census',version=1,
            **{name:self.grant[name] for name in ('task_id','master_id','source_id','target_id','source_relationship',
                'target_relationship','plan_sha256','units_sha256','source_binding','runner_id')},
            node_id=self.grant['source_id'],epoch=3,census_id='a'*32,object_count=2,total_bytes=20,
            catalog_sha256='b'*64,physical_sha256='c'*64,roots_sha256='d'*64,observed_at=self.now,expires_at=self.now+45,
            native_usage_reconciled=False,source_files_removed=False,source_drain_released=False,task_completed=False)
        self.census=p.sign(self.source,self.proof)
        self.value=dict(format=FORMAT,version=1,purpose='release-retained-source',**{name:self.grant[name] for name in IDENTITIES},
            epoch=3,runner_id=self.grant['runner_id'],unit_count=2,object_count=3,total_bytes=30,
            committed_units=1,committed_objects=1,committed_bytes=10,retained_objects=2,retained_bytes=20,
            census_sha256=digest(self.census),catalog_sha256='b'*64,roots_sha256='d'*64,master_state_sha256='e'*64,
            issued_at=self.now,expires_at=self.now+45,source_files_removed=False,ownership_changed=False,task_completed=False)

    def check(self,*,value=None,grant=None,census=None,key=None,now=None,counts=None):
        return verify(p.sign(key or self.master,value or self.value),p.public_key(self.master),grant or self.grant,
            self.counts if counts is None else counts,self.now if now is None else now,source_census=census or self.census)

    def test_valid_mixed_agreement_is_not_a_native_or_master_completion(self):
        self.assertEqual(self.check(),self.value)
        self.assertFalse(self.check()['task_completed']);self.assertFalse(self.check()['source_files_removed'])

    def test_each_counter_requires_real_integers_and_consistent_conservation(self):
        for field in ('version','epoch','unit_count','object_count','total_bytes','committed_units','committed_objects',
                      'committed_bytes','retained_objects','retained_bytes','issued_at','expires_at'):
            with self.subTest(field=field),self.assertRaises(TransferError):self.check(value={**self.value,field:True})
        for change in (dict(committed_bytes=11),dict(committed_units=0),dict(committed_objects=0),
                       dict(committed_units=3),dict(retained_bytes=0),dict(committed_bytes=-1)):
            with self.subTest(change=change),self.assertRaises(TransferError):self.check(value={**self.value,**change})

    def test_master_signature_and_source_signature_are_independently_required(self):
        with self.assertRaises(TransferError):self.check(key=self.target)
        with self.assertRaises(TransferError):self.check(census=p.sign(self.master,self.proof))
        with self.assertRaises(TransferError):self.check(census={**self.census,'payload':{**self.proof,'total_bytes':19}})

    def test_expiry_identity_epoch_and_purpose_are_bound(self):
        with self.assertRaises(TransferError):self.check(now=self.now+45)
        for field,value in (('epoch',4),('runner_id','f'*32),('source_id','f'*32),('target_id','f'*32),
                            ('source_binding','f'*64),('target_binding','f'*64),('purpose','copy')):
            with self.subTest(field=field),self.assertRaises(TransferError):self.check(grant={**self.grant,field:value})
        with self.assertRaises(TransferError):self.check(value={**self.value,'expires_at':self.now+46})

    def test_unexpected_fields_and_premature_completion_flags_are_rejected(self):
        for field in ('source_files_removed','ownership_changed','task_completed'):
            with self.subTest(field=field),self.assertRaises(TransferError):self.check(value={**self.value,field:True})
        with self.assertRaises(TransferError):self.check(value={**self.value,'extra':0})
        with self.assertRaises(TransferError):self.check(value={**self.value,'master_state_sha256':''})
        with self.assertRaises(TransferError):self.check(counts=(True,3,30))

    def test_empty_current_source_after_legitimate_deletions_keeps_original_counts(self):
        proof={**self.proof,'object_count':0,'total_bytes':0};census=p.sign(self.source,proof)
        value={**self.value,'committed_units':0,'committed_objects':0,'committed_bytes':0,
            'retained_objects':0,'retained_bytes':0,'census_sha256':digest(census)}
        result=self.check(value=value,census=census)
        self.assertEqual((result['object_count'],result['total_bytes']),(3,30))


@unittest.skipUnless(os.name=='posix','agreement authority uses native current census')
class MasterAgreementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.CurrentCancellationCatalogTests.asyncSetUp
    asyncTearDown=fixtures.CurrentCancellationCatalogTests.asyncTearDown
    new_catalog=fixtures.CurrentCancellationCatalogTests.new_catalog
    step=fixtures.CurrentCancellationCatalogTests.step
    finish=fixtures.CurrentCancellationCatalogTests.finish
    preserved=fixtures.CurrentCancellationCatalogTests.preserved
    usage=fixtures.CurrentCancellationCatalogTests.usage

    async def test_only_verified_current_catalog_authorizes_without_counter_or_peer_changes(self):
        await self.finish();before=await self.usage();calls=len(self.calls)
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted media')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read media'))):
            result=await self.catalog.authorize(self.lease)
        async with self.f.master.database.connect() as conn:task=(await conn.execute(select(s.tasks))).mappings().one()
        value=verify(result['agreement'],self.relation['peer_key'],result['grant']['payload'],
            (task['plan']['unit_count'],task['object_count'],task['total_bytes']),self.f.now,source_census=result['source_census'])
        self.assertEqual((value['committed_units'],value['committed_objects'],value['retained_objects']),(1,1,2))
        self.assertEqual(before,await self.usage());self.assertEqual(calls,len(self.calls));await self.preserved()

    async def test_unverified_master_cannot_authorize_a_native_census_alone(self):
        with self.assertRaisesRegex(TransferError,'agreement_required'):await self.catalog.authorize(self.lease)
        self.assertEqual(self.calls,[]);await self.preserved()

    async def test_expired_source_proof_cannot_be_extended_by_signing_a_new_grant(self):
        await self.finish();self.f.now+=46
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(fs.relationships).values(last_heartbeat=int(self.f.now)))
        with self.assertRaisesRegex(TransferError,'proof_invalid'):await self.catalog.authorize(self.lease)
        await self.preserved()

    async def test_changed_committed_counter_cannot_receive_release_authority(self):
        await self.finish()
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(s.tasks).values(committed_bytes=s.tasks.c.committed_bytes+1))
        with self.assertRaisesRegex(TransferError,'committed_bytes_changed'):await self.catalog.authorize(self.lease)
        await self.preserved()

    async def test_changed_current_master_ownership_cannot_receive_release_authority(self):
        await self.finish()
        async with self.f.master.database.begin() as conn:
            await conn.execute(delete(fs.global_media).where(fs.global_media.c.media_id==self.case.resources[1][0]))
        with self.assertRaisesRegex(TransferError,'current_owner_mismatch'):await self.catalog.authorize(self.lease)
        await self.preserved()

    async def test_new_runner_and_failed_audit_cannot_reuse_uncommitted_authority(self):
        await self.finish()
        with patch.object(self.f.master.store,'log',side_effect=RuntimeError('fixture audit failed')):
            with self.assertRaisesRegex(RuntimeError,'audit failed'):await self.catalog.authorize(self.lease)
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.runners).values(expires_at=0))
        self.lease=await self.catalog.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        with self.assertRaisesRegex(TransferError,'agreement_required'):await self.catalog.authorize(self.lease)
        await self.preserved()


if __name__=='__main__':unittest.main()
