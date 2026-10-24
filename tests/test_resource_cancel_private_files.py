"""Partial-copy removal truthfully records actual bytes and known absence."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.services import resource_cancel_private_files as files,resource_transfer_files as staging
from app.services.resource_transfer_protocol import TransferError


@unittest.skipUnless(os.name=='posix','requires native nofollow files')
class PrivateCopyFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.task='a'*32;self.unit=dict(unit_id='b'*64,resource_kind='media',parent_path='music/album',
            state='copying',committed_at=0,cleanup_after=0,publication={})
        self.obj=dict(logical_id='c'*64,unit_id=self.unit['unit_id'],resource_kind='media',
            target_path='music/album/one.mp3',expected_bytes=1024)
        self.proof=staging.probe(self.root,self.task,'d'*64)
        self.part=self.root/staging.DIRECTORY/self.task/self.unit['unit_id']/(self.obj['logical_id']+'.part')

    def tearDown(self):self.temp.cleanup()

    def write(self,data=b'partial'):
        descriptor,_=staging.open_stage(self.root,self.task,self.obj,self.proof)
        try:
            staging.write(descriptor,data,0);os.fsync(descriptor)
            return staging.stage_identity(os.fstat(descriptor))
        finally:os.close(descriptor)

    def inspect(self,identity=''):return files.inspect(self.root,self.task,self.unit,self.obj,self.proof,identity)
    def unlink(self,intent):return files.unlink(self.root,self.task,self.unit,self.obj,self.proof,intent)

    def test_partial_bytes_and_replay_do_not_claim_the_full_logical_size_or_read_payload(self):
        identity=self.write()
        with patch.object(os,'read',side_effect=AssertionError('payload read')),patch.object(os,'pread',side_effect=AssertionError('payload read')):
            intent=self.inspect(identity);self.unlink(intent);self.unlink(intent)
        self.assertEqual(files.validate(intent,self.task,self.unit,self.obj,self.proof),
            dict(deleted_count=1,deleted_bytes=7,absent_count=0))
        self.assertFalse(self.part.exists())

    def test_never_started_copy_is_absent_without_creating_an_empty_part_or_unit(self):
        for invalid in (None,False,0,[],{}):
            with self.assertRaises(TransferError):self.inspect(invalid)
        intent=self.inspect();self.unlink(intent);self.unlink(intent)
        self.assertFalse(self.part.parent.exists())
        self.assertEqual(files.validate(intent,self.task,self.unit,self.obj,self.proof),
            dict(deleted_count=0,deleted_bytes=0,absent_count=1))

    def test_absence_intent_does_not_allow_deleting_a_later_file(self):
        intent=self.inspect();self.write()
        with self.assertRaisesRegex(TransferError,'reappeared'):self.unlink(intent)
        self.assertEqual(self.part.read_bytes(),b'partial')

    def test_missing_previously_recorded_copy_requires_its_original_unlink_intent(self):
        identity=self.write();self.part.unlink()
        with self.assertRaisesRegex(TransferError,'stage_missing'):self.inspect(identity)

    def test_changed_partial_file_is_retained(self):
        identity=self.write();intent=self.inspect(identity);self.part.write_bytes(b'changed')
        with self.assertRaisesRegex(TransferError,'stage_changed'):self.unlink(intent)
        self.assertEqual(self.part.read_bytes(),b'changed')

    def test_nonempty_unbound_copy_is_not_adopted(self):
        self.write()
        with self.assertRaisesRegex(TransferError,'stage_changed'):self.inspect()
        self.assertEqual(self.part.read_bytes(),b'partial')

    def test_committed_or_publishing_units_cannot_use_private_removal(self):
        identity=self.write()
        for state in ('committed','publishing','published','cancelled'):
            self.unit['state']=state
            with self.assertRaises(TransferError):self.inspect(identity)
        self.assertEqual(self.part.read_bytes(),b'partial')

    def test_missing_or_replaced_task_root_does_not_prove_absence(self):
        self.part.parent.parent.rmdir()
        with self.assertRaises((OSError,TransferError)):self.inspect()
        self.assertFalse(self.part.parent.parent.exists())
