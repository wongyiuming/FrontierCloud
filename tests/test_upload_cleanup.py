import os
import tempfile
import time
import unittest
from pathlib import Path

from app.services.upload_cleanup import cleanup_stale_upload_parts


class StaleUploadCleanupTests(unittest.TestCase):
    def test_only_expired_upload_parts_are_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "nested"
            nested.mkdir()
            stale = nested / ".upload-stale.part"
            fresh = nested / ".upload-fresh.part"
            unrelated = nested / "keep.part"
            stale.write_bytes(b"stale")
            fresh.write_bytes(b"fresh")
            unrelated.write_bytes(b"keep")
            old_timestamp = time.time() - 600
            os.utime(stale, (old_timestamp, old_timestamp))

            removed_count, removed_bytes = cleanup_stale_upload_parts(
                root,
                max_age_seconds=300,
            )

            self.assertEqual((removed_count, removed_bytes), (1, 5))
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    def test_cleanup_does_not_descend_into_symlinked_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            scan_root = root / "media"
            outside.mkdir()
            scan_root.mkdir()
            stale = outside / ".upload-outside.part"
            stale.write_bytes(b"outside")
            old_timestamp = time.time() - 600
            os.utime(stale, (old_timestamp, old_timestamp))
            link = scan_root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks require additional privileges")

            removed_count, removed_bytes = cleanup_stale_upload_parts(
                scan_root,
                max_age_seconds=300,
            )

            self.assertEqual((removed_count, removed_bytes), (0, 0))
            self.assertTrue(stale.exists())


if __name__ == "__main__":
    unittest.main()
