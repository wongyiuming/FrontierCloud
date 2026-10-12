"""Actual Linux restoration with metadata and newly created parent barriers."""
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import operator_backup as backup
from tests.test_operator_backup import FakeDocker, populate


@unittest.skipUnless(sys.platform == "linux", "Linux directory/inode fsync and ownership")
class OperatorRestoreDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.uid = 10001 if os.geteuid() == 0 else os.getuid()
        self.gid = 10001 if os.geteuid() == 0 else os.getgid()
        source = self.root / "source"
        source.mkdir(mode=0o750)
        (source / "nested").mkdir(mode=0o750)
        self.contents = b"\x00\xffdurable-private-state"
        original = source / "nested/node-vault.key"
        original.write_bytes(self.contents)
        os.chown(original, self.uid, self.gid)
        os.chmod(original, 0o640)
        self.timestamp = 1_700_000_123
        os.utime(original, (self.timestamp, self.timestamp))
        self.archive = self.root / "state.tar"
        self.inventory = backup.archive_tree(source, self.archive)

    def test_new_bind_root_and_every_missing_ancestor_receive_parent_barriers(self):
        target = self.root / "new/deployment/data"
        calls = []
        original = backup.fsync_directory
        def synced(path):
            calls.append(path)
            original(path)
        with patch.object(backup, "fsync_directory", side_effect=synced):
            backup.restore_archive(self.archive, target, self.inventory)
        self.assertEqual(calls, [target.parent, target.parent.parent, self.root])
        restored = target / "nested/node-vault.key"
        self.assertEqual(restored.read_bytes(), self.contents)
        info = restored.stat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), int(info.st_mtime)),
                         (self.uid, self.gid, 0o640, self.timestamp))

    def test_final_file_fsync_observes_restored_owner_mode_and_timestamp(self):
        target = self.root / "restored"
        observations = []
        original = os.fsync
        def synced(descriptor):
            info = os.fstat(descriptor)
            if stat.S_ISREG(info.st_mode):
                observations.append((os.readlink(f"/proc/self/fd/{descriptor}"), info.st_uid, info.st_gid,
                                     stat.S_IMODE(info.st_mode), int(info.st_mtime), info.st_size))
            original(descriptor)
        with patch.object(backup.os, "fsync", side_effect=synced):
            backup.restore_archive(self.archive, target, self.inventory)
        self.assertEqual(observations, [(str(target / "nested/node-vault.key"), self.uid, self.gid,
                                        0o640, self.timestamp, len(self.contents))])

    def test_parent_barrier_failure_prevents_restore_success_and_protects_partial_state(self):
        target = self.root / "new/deployment/data"
        original = backup.fsync_directory
        def failing(path):
            if path == self.root:
                raise OSError("injected ancestor fsync failure")
            original(path)
        with patch.object(backup, "fsync_directory", side_effect=failing):
            with self.assertRaisesRegex(OSError, "ancestor fsync"):
                backup.restore_archive(self.archive, target, self.inventory)
        self.assertEqual((target / "nested/node-vault.key").read_bytes(), self.contents)
        with self.assertRaisesRegex(backup.BackupError, "not empty"):
            backup.restore_archive(self.archive, target, self.inventory)

    def test_final_metadata_fsync_failure_propagates_instead_of_reporting_success(self):
        target = self.root / "restored"
        original = os.fsync
        def failing(descriptor):
            info = os.fstat(descriptor)
            if stat.S_ISREG(info.st_mode) and info.st_uid == self.uid:
                raise OSError("injected metadata durability failure")
            original(descriptor)
        with patch.object(backup.os, "fsync", side_effect=failing):
            with self.assertRaisesRegex(OSError, "metadata durability"):
                backup.restore_archive(self.archive, target, self.inventory)
        self.assertEqual((target / "nested/node-vault.key").read_bytes(), self.contents)

    def test_existing_root_does_not_create_ancestor_entries_or_follow_symlinks(self):
        target = self.root / "existing"
        target.mkdir()
        with patch.object(backup, "fsync_directory", wraps=backup.fsync_directory) as synced:
            backup.restore_archive(self.archive, target, self.inventory)
        self.assertEqual(synced.call_count, 0)
        alias = self.root / "alias"
        alias.symlink_to(self.root / "outside", target_is_directory=True)
        with self.assertRaisesRegex(backup.BackupError, "symbolic"):
            backup.restore_archive(self.archive, alias / "data", self.inventory)
        self.assertFalse((self.root / "outside").exists())

    def test_full_sqlite_restore_syncs_final_database_owner_and_mode(self):
        guard = patch.object(backup, "assert_no_open_writers")
        with guard:
            source_docker = FakeDocker(self.root, "source-project")
            source = backup.Plan.load(source_docker)
            populate(source)
            database = source.resources["control"].root / "frontiercloud.db"
            os.chown(database, self.uid, self.gid)
            os.chmod(database, 0o640)
            snapshot = self.root / "snapshot"
            backup.backup(source, snapshot)
            target_docker = FakeDocker(self.root, "target-project", initialized=False)
            target = backup.Plan.load(target_docker, restoring=True, snapshot_resources=set(source.resources))
            destination = self.root / "new/full/deployment/data"
            target.resources["control"].root = destination
            target.resources["control"].source = str(destination)
            observations = []
            original = os.fsync
            def synced(descriptor):
                info = os.fstat(descriptor)
                name = os.readlink(f"/proc/self/fd/{descriptor}")
                if name == str(destination / "frontiercloud.db"):
                    observations.append((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)))
                original(descriptor)
            with patch.object(backup.os, "fsync", side_effect=synced):
                backup.restore(target, snapshot)
            self.assertTrue(observations)
            self.assertEqual(observations[-1], (self.uid, self.gid, 0o640))
            self.assertFalse(target_docker.stopped)


if __name__ == "__main__":
    unittest.main()
