"""Real retained Source signatures without authorizing any Target deletion."""
import os
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select

from app.services import media_crypto,media_crypto_schema as cs,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_authority import UNIT_FIELDS
from app.services.resource_cancel_source import CancellationSource,reply,verify
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_authority as fixtures


@unittest.skipUnless(os.name=='posix','cancellation Source proof uses native Linux publication')
class CancellationSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.CancellationAuthorityTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f;await self.fixture.prepare();await self.fixture.cancelling();self.lease=self.fixture.lease
        self.service=self.new_service()

    async def asyncTearDown(self):await self.fixture.asyncTearDown()

    def new_service(self):
        return CancellationSource(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def finish(self,service=None):
        for _ in range(12):
            result=await (service or self.service).advance(self.lease,self.fixture.unit)
            if result['phase']=='verified':return result['proof']
        self.fail('Source verification did not complete')

    async def domain(self):
        async with self.f.master.database.connect() as conn:
            return {table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()]
                for table in (fs.identity,fs.global_media,fs.storage_members,cs.encryption,s.capacity,s.scopes,s.drains)}

    async def context(self,proof):
        async with self.f.master.database.begin() as conn:task=await self.service.transfer.journal._fenced(conn,self.lease)
        relations=await self.service.transfer.relations(task)
        grant=self.service.transfer._grant(task,relations,self.lease.epoch,self.lease.runner_id,'inspect')['payload']
        selected={name:proof['authorization']['payload'][name] for name in UNIT_FIELDS}
        return grant,selected

    async def test_actual_source_signature_survives_master_transport_and_preserves_domain_rows(self):
        before=await self.domain()
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('media decryption')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('media premaster'))):
            proof=await self.finish()
        grant,unit=await self.context(proof)
        result=verify(proof['source_reply'],self.fixture.master_key(),proof['authorization'],grant,unit,self.f.now)
        self.assertEqual(result['phase'],'verified');self.assertFalse(result['ownership_changed'])
        self.assertEqual(await self.domain(),before);await self.fixture.originals()
        async with self.f.master.database.connect() as conn:
            stored=await conn.scalar(select(s.units.c.publication))
        self.assertEqual(stored['cancel_source'],proof)

    async def test_master_resigning_a_source_payload_cannot_replace_source_authority(self):
        proof=await self.finish();grant,unit=await self.context(proof)
        forged=p.sign(self.f.master.store.unseal(self.f.master.store.node['private_key']),proof['source_reply']['payload'])
        with self.assertRaisesRegex(TransferError,'source_proof_invalid'):
            verify(forged,self.fixture.master_key(),proof['authorization'],grant,unit,self.f.now)
        await self.fixture.originals()

    async def test_signed_wrong_scope_partial_and_boolean_proofs_are_rejected(self):
        proof=await self.finish();grant,unit=await self.context(proof)
        private=self.f.source.store.unseal(self.f.source.store.node['private_key'])
        for values in ({'version':True},{'node_id':'a'*32},{'epoch':True},{'processed_objects':0},
                {'ownership_changed':0},{'phase':'membership','receipt_sha256':''},{'receipt_sha256':''},
                {'expires_at':proof['authorization']['payload']['expires_at']+1}):
            forged=p.sign(private,{**proof['source_reply']['payload'],**values})
            with self.subTest(values=values),self.assertRaises(TransferError):
                verify(forged,self.fixture.master_key(),proof['authorization'],grant,unit,self.f.now)
        with self.assertRaisesRegex(TransferError,'source_proof_expired'):
            reply(proof['source_reply'],grant,unit,proof['source_reply']['payload']['expires_at'])
        with self.assertRaisesRegex(TransferError,'authorization_expired'):
            verify(proof['source_reply'],self.fixture.master_key(),proof['authorization'],grant,unit,
                proof['authorization']['payload']['expires_at'])
        await self.fixture.originals()

    async def test_changed_source_is_preserved_and_cannot_produce_a_valid_shadow_removal_proof(self):
        _,path,data=self.fixture.fixture.resources[0];path.write_bytes(b'changed source original')
        with self.assertRaises(TransferError):await self.finish()
        self.assertEqual(path.read_bytes(),b'changed source original')
        self.assertEqual((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).read_bytes(),data)
        async with self.f.master.database.connect() as conn:
            self.assertNotIn('cancel_source',await conn.scalar(select(s.units.c.publication)))

    async def test_unknown_source_sibling_is_preserved_and_blocks_completion(self):
        _,path,_=self.fixture.fixture.resources[0];unknown=path.parent/'unknown.mp3';unknown.write_bytes(b'never delete this')
        with self.assertRaisesRegex(TransferError,'unknown_entry'):await self.finish()
        self.assertEqual(unknown.read_bytes(),b'never delete this');await self.fixture.originals()

    async def test_lost_source_reply_is_reverified_after_coordinator_restart_without_releasing_resources(self):
        original=self.f.transport.request;failed=False;before=await self.domain()
        async def lose(origin,path,**kwargs):
            nonlocal failed
            value=await original(origin,path,**kwargs)
            if path.endswith('/verify-source') and value['payload']['phase']=='verified' and not failed:
                failed=True;raise httpx.ReadTimeout('controlled lost Source signature')
            return value
        self.f.transport.request=lose
        try:
            with self.assertRaises(httpx.ReadTimeout):await self.finish()
        finally:self.f.transport.request=original
        self.assertTrue(failed);self.assertEqual(await self.domain(),before)
        proof=await self.finish(self.new_service());grant,unit=await self.context(proof)
        verify(proof['source_reply'],self.fixture.master_key(),proof['authorization'],grant,unit,self.f.now)
        self.assertEqual(await self.domain(),before);await self.fixture.originals()

    async def test_master_proof_audit_rollback_keeps_the_proof_unpublished_until_retry(self):
        log=self.f.master.store.log;before=await self.domain()
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_cancel_source_observed' and kwargs.get('phase')=='verified':raise OSError('controlled proof audit failure')
            return await log(conn,action,*args,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.finish()
        async with self.f.master.database.connect() as conn:
            self.assertNotIn('cancel_source',await conn.scalar(select(s.units.c.publication)))
        self.assertEqual(await self.domain(),before)
        await self.finish(self.new_service());self.assertEqual(await self.domain(),before);await self.fixture.originals()
