import tempfile
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


if __name__ == "__main__":
    unittest.main()
