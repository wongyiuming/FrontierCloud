"""Separate-process lease evidence, including crash, race and symlink rejection."""
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from app.services.upload_claims import claim, UploadBusy


def hold(root, ready):
    with claim('a' * 32, root=root):
        ready.set()
        import time
        time.sleep(60)


@unittest.skipIf(os.name == 'nt', 'Product upload leases use Linux kernel flock')
class UploadClaims(unittest.TestCase):
    def test_live_writer_and_process_death(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context('spawn')
            ready = context.Event()
            process = context.Process(target=hold, args=(directory, ready))
            process.start()
            try:
                self.assertTrue(ready.wait(10))
                with self.assertRaises(UploadBusy):
                    with claim('a' * 32, root=directory):
                        self.fail('Active upload was incorrectly released')
                process.kill()
                process.join(10)
                self.assertFalse(process.is_alive())
                with claim('a' * 32, root=directory):
                    pass
            finally:
                if process.is_alive():
                    process.kill()
                    process.join(10)

    def test_identity_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                with claim('../escape', root=directory):
                    pass
            outside = Path(directory) / 'outside'
            outside.write_text('preserve')
            (Path(directory) / ('a' * 32)).symlink_to(outside)
            with self.assertRaises(OSError):
                with claim('a' * 32, root=directory):
                    pass
            self.assertEqual(outside.read_text(), 'preserve')
