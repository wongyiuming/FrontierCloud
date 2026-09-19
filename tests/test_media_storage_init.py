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
            with patch.object(media_storage_init.os, "chown", create=True) as chown:
                media_storage_init.initialize_media_storage(data_root, uid=10001, gid=10001)

            for relative in media_storage_init.MEDIA_DIRECTORIES:
                self.assertTrue((data_root / relative).is_dir())
            owned = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertIn(data_root, owned)
            self.assertIn(existing, owned)
            self.assertTrue(all(call.args[1:] == (10001, 10001) for call in chown.call_args_list))


if __name__ == "__main__":
    unittest.main()
