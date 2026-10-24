"""Native shadow unlink primitives, not end-to-end cancellation authority."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services import resource_cancel_files as cancel,resource_publish_files as publication,resource_transfer_files as staging
from app.services.resource_observation import digest,native
from app.services.resource_transfer_protocol import TransferError


@unittest.skipUnless(os.name=='posix','shadow rollback requires native Linux nofollow descriptors')
class ShadowFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.base=Path(self.temporary.name)
        self.root=self.base/'target';self.root.mkdir()
        self.task='a'*32;self.unit_id='b'*64;self.logical='c'*64
        self.data=AESGCM(bytes(32)).encrypt(bytes(12),b'opaque original ciphertext fixture'*1024,b'fixture')
        self.original=self.base/'source-original';self.original.write_bytes(self.data)
        self.root_proof=staging.probe(self.root,self.task,'d'*64)
        self.obj=dict(unit_id=self.unit_id,logical_id=self.logical,resource_kind='media',target_path='music/album/file.mp3',
            expected_bytes=len(self.data))
        self.unit=dict(unit_id=self.unit_id,resource_kind='media',parent_path='music/album',state='verified',
            committed_at=0,cleanup_after=0,publication={})
        descriptor,_=staging.open_stage(self.root,self.task,self.obj,self.root_proof)
        try:staging.write(descriptor,self.data,0);os.fsync(descriptor);self.verified=native(os.fstat(descriptor))
        finally:os.close(descriptor)
        self.private=self.root/staging.DIRECTORY/self.task/self.unit_id
        self.part=self.private/(self.logical+'.part')
        self.public=self.root/self.obj['target_path']

    def tearDown(self):
        self.assertEqual(self.original.read_bytes(),self.data)
        self.temporary.cleanup()

    def prepare(self):
        self.unit['publication']=publication.prepare(self.root,self.task,self.unit,self.root_proof)
        self.unit['state']='publishing'

    def assemble(self):
        self.prepare()
        self.assembled=publication.assemble(self.root,self.task,self.unit_id,self.obj,self.root_proof,
            self.unit['publication'],self.verified)
        return self.private/'ready/file.mp3'

    def publish(self):
        self.assemble()
        descriptor=publication.open_directory(self.root,self.task,self.unit,self.root_proof,self.unit['publication'],published=False)
        try:self.unit['publication']['scan_version']=digest(native(os.fstat(descriptor)))
        finally:os.close(descriptor)
        publication.move_directory(self.root,self.task,self.unit,self.root_proof,self.unit['publication'])
        # This is deliberately the pre-response journal state. Location must
        # reconcile an actual rename whose SQL acknowledgement was lost.
        self.unit['publication']['step']='move'
        return self.public

    def observe(self,*,assembled=False,expected=None):
        return cancel.inspect_object(self.root,self.task,self.unit,self.obj,self.root_proof,
            expected or (self.assembled if assembled else self.verified),assembled=assembled)

    def unlink(self,intent):
        cancel.unlink_object(self.root,self.task,self.unit,self.obj,self.root_proof,intent)

    def remove_directory(self):
        cancel.remove_directory(self.root,self.task,self.unit,self.obj,self.root_proof)

    def test_opaque_staging_shadow_unlink_and_intent_replay_read_no_payload(self):
        with (patch.object(os,'read',side_effect=AssertionError('payload read')),
                patch.object(os,'pread',side_effect=AssertionError('payload read')),
                patch.object(os,'write',side_effect=AssertionError('payload copy'))):
            intent=self.observe();self.assertEqual(intent['area'],'part');self.unlink(intent);self.unlink(intent)
        self.assertFalse(self.part.exists())
        with self.assertRaisesRegex(TransferError,'shadow_file_missing'):self.observe()

    def test_private_assembly_shadow_and_empty_directory_are_removed_without_ancestors(self):
        shadow=self.assemble();intent=self.observe(assembled=True);self.assertEqual(intent['area'],'ready')
        self.unlink(intent);self.assertFalse(shadow.exists());self.remove_directory();self.remove_directory()
        self.assertTrue(self.private.is_dir());self.assertTrue((self.root/'music').is_dir())

    def test_published_shadow_with_lost_move_reply_keeps_sibling_directory_and_source(self):
        shadow=self.publish();other=self.root/'music/other';other.mkdir();(other/'keep.mp3').write_bytes(b'untouched')
        intent=self.observe(assembled=True);self.assertEqual(intent['area'],'public');self.unlink(intent)
        self.remove_directory();self.remove_directory()
        self.assertFalse(shadow.parent.exists());self.assertEqual((other/'keep.mp3').read_bytes(),b'untouched')
        self.assertTrue(other.parent.is_dir())

    def test_recording_namespace_and_opaque_ciphertext_use_the_same_owned_unlink(self):
        self.logical='f'*32
        renamed=self.private/(self.logical+'.part');self.part.rename(renamed);self.part=renamed
        self.verified=native(self.part.stat())
        self.obj.update(logical_id=self.logical,resource_kind='recording',target_path='d'*32+'/'+'e'*32+'/'+self.logical+'.bin')
        self.unit.update(resource_kind='recording',parent_path=self.obj['target_path'].rsplit('/',1)[0])
        self.public=self.root/self.obj['target_path'];self.publish()
        self.unlink(self.observe(assembled=True));self.remove_directory()
        self.assertFalse(self.public.parent.exists());self.assertTrue(self.public.parent.parent.is_dir())

    def test_original_empty_target_is_preserved_when_private_assembly_is_cancelled(self):
        self.public.parent.mkdir(parents=True);original=native(self.public.parent.stat())
        self.assemble();self.unlink(self.observe(assembled=True));self.remove_directory()
        self.assertEqual(native(self.public.parent.stat()),original)

    def test_file_changed_after_observation_is_preserved_with_the_intent(self):
        shadow=self.publish();intent=self.observe(assembled=True)
        with shadow.open('ab') as output:output.write(b'new unknown bytes')
        with self.assertRaisesRegex(TransferError,'reappeared_or_changed'):self.unlink(intent)
        self.assertEqual(shadow.read_bytes(),self.data+b'new unknown bytes')

    def test_replaced_file_is_preserved_before_any_unlink(self):
        shadow=self.publish();kept=shadow.with_name('preserved-old');shadow.rename(kept);shadow.write_bytes(b'foreign replacement')
        with self.assertRaisesRegex(TransferError,'shadow_file_changed'):self.observe(assembled=True)
        self.assertEqual(shadow.read_bytes(),b'foreign replacement');self.assertEqual(kept.read_bytes(),self.data)

    def test_reappeared_file_after_unlink_is_never_treated_as_completed_replay(self):
        shadow=self.publish();intent=self.observe(assembled=True);self.unlink(intent);shadow.write_bytes(self.data)
        with self.assertRaisesRegex(TransferError,'reappeared_or_changed'):self.unlink(intent)
        self.assertEqual(shadow.read_bytes(),self.data)

    def test_unknown_entry_prevents_directory_removal_without_recursive_cleanup(self):
        shadow=self.publish();self.unlink(self.observe(assembled=True));unknown=shadow.parent/'keep-this';unknown.write_bytes(b'keep')
        with self.assertRaisesRegex(TransferError,'unknown_publication_entry'):self.remove_directory()
        self.assertEqual(unknown.read_bytes(),b'keep')

    def test_symlink_shadow_never_touches_its_source_target(self):
        self.part.unlink();self.part.symlink_to(self.original)
        with self.assertRaisesRegex(TransferError,'shadow_file_changed'):self.observe()
        self.assertTrue(self.part.is_symlink())

    def test_extra_hard_link_is_preserved_until_explicit_reconciliation(self):
        kept=self.base/'other-link';os.link(self.part,kept)
        with self.assertRaisesRegex(TransferError,'shadow_file_changed'):self.observe()
        self.assertTrue(self.part.exists());self.assertEqual(kept.read_bytes(),self.data)

    def test_all_committed_indicators_refuse_removal_before_native_io(self):
        intent=self.observe()
        for values in ({'state':'committed'},{'state':'cleanup_pending'},{'state':'cleaned'},
                {'committed_at':1},{'cleanup_after':3600}):
            with self.subTest(values=values),patch.dict(self.unit,values):
                with self.assertRaisesRegex(TransferError,'uncommitted_unit_required'):self.unlink(intent)
        self.assertEqual(self.part.read_bytes(),self.data)

    def test_unknown_public_replacement_is_preserved_even_if_our_private_ready_exists(self):
        self.assemble();self.public.parent.mkdir();self.public.write_bytes(b'foreign bytes')
        with self.assertRaisesRegex(TransferError,'foreign_target_directory'):self.observe(assembled=True)
        self.assertEqual(self.public.read_bytes(),b'foreign bytes')

    def test_replaced_private_task_root_cannot_authorize_public_unlink(self):
        self.publish();intent=self.observe(assembled=True)
        task_root=self.private.parent;task_root.rename(task_root.with_name('kept-task'))
        task_root.mkdir(mode=0o700)
        with self.assertRaisesRegex(TransferError,'staging_root_changed'):self.unlink(intent)
        self.assertEqual(self.public.read_bytes(),self.data)

    def test_rename_of_public_directory_refuses_unlink_through_a_new_directory(self):
        self.publish();intent=self.observe(assembled=True);saved=self.public.parent.with_name('kept-original')
        self.public.parent.rename(saved);self.public.parent.mkdir();self.public.write_bytes(b'new content')
        with self.assertRaisesRegex(TransferError,'foreign_target_directory'):self.unlink(intent)
        self.assertEqual(self.public.read_bytes(),b'new content');self.assertEqual((saved/self.public.name).read_bytes(),self.data)

    def test_wrong_task_or_object_intent_cannot_be_reused(self):
        intent=self.observe()
        for values in ({'task_id':'d'*32},{'logical_id':'e'*64},{'unit_id':'f'*64},{'version':True}):
            with self.subTest(values=values),self.assertRaisesRegex(TransferError,'unlink_intent_invalid'):
                self.unlink({**intent,**values})
        self.assertEqual(self.part.read_bytes(),self.data)

    def test_fsync_failure_after_unlink_retries_the_durable_absence_without_a_new_observation(self):
        intent=self.observe()
        with patch.object(os,'fsync',side_effect=OSError('controlled durability failure')):
            with self.assertRaises(OSError):self.unlink(intent)
        self.assertFalse(self.part.exists());self.unlink(intent)

    def test_observation_and_refusal_paths_leave_no_open_descriptors(self):
        self.publish();before=len(os.listdir('/proc/self/fd'))
        for _ in range(30):
            self.observe(assembled=True)
            with self.assertRaisesRegex(TransferError,'unknown_publication_entry'):self.remove_directory()
        self.assertEqual(len(os.listdir('/proc/self/fd')),before)
