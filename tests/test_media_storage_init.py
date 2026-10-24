import tempfile
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import media_storage_init


class MediaStorageInitializationTests(unittest.TestCase):
    def test_initializer_creates_all_managed_roots_and_assigns_runtime_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "data"
            existing = data_root / "media" / "music" / "album" / "track.mp3"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"media")
            current_owner = data_root.stat()
            target_uid = current_owner.st_uid + 1
            target_gid = current_owner.st_gid + 1
            with patch.object(media_storage_init.os, "chown", create=True) as chown:
                media_storage_init.initialize_media_storage(data_root, uid=target_uid, gid=target_gid)

            for relative in media_storage_init.MEDIA_DIRECTORIES:
                self.assertTrue((data_root / relative).is_dir())
            owned = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertIn(data_root, owned)
            self.assertIn(existing, owned)
            self.assertTrue(all(call.args[1:3] == (target_uid, target_gid) for call in chown.call_args_list))

    def test_initializer_keeps_matching_ownership_without_redundant_chown(self):
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "data"
            data_root.mkdir()
            owner = data_root.stat()
            with patch.object(media_storage_init.os, "chown", create=True) as chown:
                media_storage_init.initialize_media_storage(data_root, uid=owner.st_uid, gid=owner.st_gid)

            chown.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', 'POSIX search permissions')
    def test_private_root_gets_only_edge_search_without_broadening_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'data';root.mkdir(mode=0o700)
            secret=root/'private-key';secret.write_bytes(b'private');secret.chmod(0o600)
            owner=root.stat()
            media_storage_init.initialize_media_storage(root,uid=owner.st_uid,gid=owner.st_gid)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode),0o711)
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode),0o600)
            self.assertEqual(secret.read_bytes(),b'private')

    @unittest.skipUnless(sys.platform == 'linux', 'POSIX search permissions')
    def test_native_storage_has_no_nginx_reader_and_retains_private_root_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'data';root.mkdir(mode=0o700);owner=root.stat()
            media_storage_init.initialize_media_storage(root,uid=owner.st_uid,gid=owner.st_gid,edge_readers=False)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode),0o700)

    def test_main_selects_edge_traversal_only_for_business(self):
        for role,expected in (('',True),('only_stroge',False)):
            with patch.dict(os.environ,{'DEPLOYMENT_MODE':role}),patch.object(media_storage_init,'initialize_media_storage') as initialize:
                media_storage_init.main()
            initialize.assert_called_once_with(edge_readers=expected)

    @unittest.skipUnless(sys.platform == 'linux', 'POSIX symlinks')
    def test_root_symlink_is_refused_without_changing_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'owner';root.mkdir(mode=0o700)
            linked=Path(directory)/'data';linked.symlink_to(root,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'real directory'):
                media_storage_init.initialize_media_storage(linked)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode),0o700)
            self.assertEqual(list(root.iterdir()),[])


if __name__ == "__main__":
    unittest.main()
