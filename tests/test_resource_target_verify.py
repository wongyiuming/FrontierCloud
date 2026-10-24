"""Post-commit Target re-observation through signed controls and real SQL/files."""
import os
import hashlib
import unittest
from unittest.mock import patch

from sqlalchemy import select,text,update

from app.services import media_crypto,media_crypto_schema as cs,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_target_verify import TargetVerification
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cutover as fixtures


@unittest.skipUnless(os.name=='posix','target verification requires native publication')
class TargetVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.CutoverTests('runTest');await self.fixture.asyncSetUp()
        self.f=self.fixture.f;self.service=self.fixture.service;self.replacements=[];self.after=None
        self.original=self.f.transport.request;self.f.transport.request=self.control

    async def asyncTearDown(self):
        for replacement in self.replacements:await replacement.close()
        await self.fixture.asyncTearDown()

    async def control(self,origin,path,**kwargs):
        if not path.endswith('/verify-target'):return await self.original(origin,path,**kwargs)
        peer=self.f.target;self.assertEqual(origin,peer.store.node['endpoint'])
        self.assertEqual((kwargs['credential'],kwargs['relation']),(peer.credential,peer.relationship))
        async with peer.database.connect() as conn:
            relation=dict((await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==peer.relationship))).mappings().one())
        result=await TargetVerification(peer.transfers).advance(peer.store.node,relation,**kwargs['value'])
        if self.after:await self.after(result)
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)

    async def prepare(self):
        await self.fixture.prepare();self.unit=self.fixture.unit_id
        self.lease,_=await self.service.commit(self.fixture.lease,self.unit)
        await self.service.acknowledge_one(self.lease,self.unit);await self.service.acknowledge_one(self.lease,self.unit)

    async def step(self):return await self.service.verify_target(self.lease,self.unit)

    async def finish(self):
        for _ in range(16):
            result=await self.step()
            if result['target_verified']:return result
        self.fail('target verification did not converge')

    def path(self,index=0):
        original=self.fixture.resources[index][1]
        return self.f.target.roots['media']/original.relative_to(self.f.source.roots['media'])

    async def retained(self):
        for _,path,data in self.fixture.resources:self.assertEqual(path.read_bytes(),data)
        for peer in (self.f.source,self.f.target):
            async with peer.database.connect() as conn:self.assertEqual(await conn.scalar(select(s.units.c.state)),'committed')

    async def test_fresh_plain_and_cipher_proof_preserves_bytes_keys_ownership_and_quota(self):
        await self.prepare();before=await self.fixture.members()
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read payload'))):
            result=await self.finish()
        self.assertTrue(result['target_verified']);self.assertFalse(result['source_files_removed'])
        self.assertEqual(await self.fixture.members(),before);await self.retained()
        async with self.f.master.database.connect() as conn:
            publication=await conn.scalar(select(s.units.c.publication))
            proof=p.verify(p.public_key(self.f.target.store.unseal(self.f.target.store.node['private_key'])),
                publication['target_live_reply']['proof'])
            self.assertEqual(proof['commit_sha256'],publication['commit_sha256'])
            self.assertGreater(proof['expires_at'],self.f.now)
            self.assertEqual(set(publication['native_acknowledgements']),{'source','target'})
        for index,(_,_,data) in enumerate(self.fixture.resources):self.assertEqual(self.path(index).read_bytes(),data)

    async def test_in_place_rewrite_even_with_restored_mtime_cannot_get_a_fresh_proof(self):
        await self.prepare();path=self.path();info=path.stat()
        path.write_bytes(b'x'*info.st_size);os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
        with self.assertRaisesRegex(TransferError,'file_changed'):await self.finish()
        self.assertEqual(path.read_bytes(),b'x'*info.st_size);await self.retained()
        self.assertIsNone(self.f.target.transfers.target_verification)

    async def test_registry_and_encryption_changes_are_not_repaired_or_ignored(self):
        await self.prepare()
        async with self.f.target.database.begin() as conn:
            identifier=await conn.scalar(text('SELECT media_id FROM media_objects LIMIT 1'))
            await conn.execute(text('UPDATE media_objects SET path_locator=:value WHERE media_id=:id'),dict(value='0'*64,id=identifier))
        with self.assertRaisesRegex(TransferError,'registry_changed'):await self.finish()
        async with self.f.target.database.begin() as conn:
            rows=(await conn.execute(text('SELECT media_id,media_path FROM media_objects'))).mappings().all()
            for row in rows:
                await conn.execute(text('UPDATE media_objects SET path_locator=:value WHERE media_id=:id'),
                    dict(value=hashlib.sha256(row['media_path'].encode()).hexdigest(),id=row['media_id']))
            await conn.execute(update(cs.encryption).values(descriptor_json=None))
        with self.assertRaisesRegex(TransferError,'encryption_changed'):await self.finish()
        async with self.f.target.database.connect() as conn:self.assertIsNone(await conn.scalar(select(cs.encryption.c.descriptor_json)))
        await self.retained()

    async def test_unknown_sibling_is_preserved_and_blocks_the_proof(self):
        await self.prepare();unknown=self.path().parent/'unknown.bin';unknown.write_bytes(b'keep unknown data')
        with self.assertRaisesRegex(TransferError,'directory_changed'):await self.finish()
        self.assertEqual(unknown.read_bytes(),b'keep unknown data');await self.retained()

    async def test_replacement_transfer_service_restarts_scan_from_durable_committed_state(self):
        await self.prepare();await self.step();old=self.f.target.transfers;scan=old.target_verification
        await old.close();self.assertIsNone(scan.descriptor)
        replacement=TransferPeers(self.f.target.store,root_provider=lambda:self.f.target.roots,traffic=self.f.traffic,wall=lambda:self.f.now)
        self.replacements.append(replacement);self.f.target.transfers=replacement
        await self.finish();await self.retained();self.assertIsNone(replacement.target_verification)

    async def test_native_audit_failure_retains_previous_state_and_retry_reobserves(self):
        await self.prepare();await self.step();await self.step();log=self.f.target.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_target_live_verified':raise OSError('injected native audit failure')
            return await log(conn,action,actor,**kwargs)
        with patch.object(self.f.target.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.step()
        self.assertIsNone(self.f.target.transfers.target_verification)
        async with self.f.target.database.connect() as conn:
            self.assertNotIn('target_live_verification',await conn.scalar(select(s.units.c.publication)))
        await self.finish();await self.retained()

    async def test_expired_reply_boolean_progress_and_wrong_signer_are_refused(self):
        await self.prepare()
        async def boolean(result):result['processed_objects']=True
        self.after=boolean
        with self.assertRaisesRegex(TransferError,'reply_invalid'):await self.step()
        async def wrong_signer(result):
            if result['proof'] is not None:
                result['proof']=p.sign(self.f.master.store.unseal(self.f.master.store.node['private_key']),result['proof']['payload'])
        self.after=wrong_signer
        with self.assertRaisesRegex(TransferError,'proof_invalid'):await self.finish()
        async def expired(result):self.f.now+=46
        self.after=expired
        with self.assertRaisesRegex(TransferError,'reply_invalid'):await self.step()
        self.after=None;await self.finish();await self.retained()

    async def test_unacknowledged_native_commit_is_not_enough_for_target_verification(self):
        await self.prepare()
        async with self.f.master.database.begin() as conn:
            publication=await conn.scalar(select(s.units.c.publication))
            publication={**publication,'native_acknowledgements':{'target':publication['native_acknowledgements']['target']}}
            await conn.execute(update(s.units).values(publication=publication))
        with self.assertRaisesRegex(TransferError,'requires_native_commit'):await self.step()
        await self.service.acknowledge_one(self.lease,self.unit);await self.finish();await self.retained()

    async def test_lost_master_audit_after_native_success_requires_fresh_observation_on_retry(self):
        await self.prepare();await self.step();await self.step();log=self.f.master.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_target_verification_step':raise OSError('injected Master audit failure')
            return await log(conn,action,actor,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.step()
        self.assertIsNone(self.f.target.transfers.target_verification)
        async with self.f.master.database.connect() as conn:
            self.assertIsNone((await conn.scalar(select(s.units.c.publication)))['target_live_reply']['proof'])
        self.assertEqual((await self.step())['phase'],'objects')
        await self.finish();await self.retained()

    async def test_recording_live_proof_keeps_target_relationship_namespace_and_opaque_footer(self):
        await self.fixture.test_recording_parent_rebind_preserves_opaque_footer_and_current_master_business()
        self.lease,self.unit=self.fixture.lease,self.fixture.unit_id
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read payload'))):
            await self.finish()
        await self.retained()
        for recording,source,data in self.fixture.resources:
            target=self.f.target.roots['recordings']/self.f.target.relationship/source.parent.name/(recording+'.bin')
            self.assertEqual(target.read_bytes(),data)
