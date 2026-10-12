import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from app.services import runtime_gate as gate


@unittest.skipUnless(sys.platform == "linux", "production single-writer gate is Linux flock")
class RuntimeGateTests(unittest.TestCase):
    def test_second_web_or_operator_process_is_excluded_and_release_is_reusable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lease = gate.acquire(root, create=False)
            with self.assertRaises(gate.RuntimeBusy):
                gate.acquire(root, create=False)
            program = "from app.services.runtime_gate import acquire; import sys; acquire(sys.argv[1],create=False)"
            result = subprocess.run([sys.executable, "-c", program, str(root)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"RuntimeBusy", result.stderr)
            lease.close()
            lease.close()
            with gate.acquire(root, create=False):
                self.assertEqual((root / gate.LOCK_NAME).stat().st_mode & 0o777, 0o600)
                if os.geteuid() == 0:
                    self.assertEqual((root / gate.LOCK_NAME).stat().st_uid, 10001)

    def test_symlink_hardlink_or_broad_permission_lease_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / gate.LOCK_NAME
            outside = root / "other"
            outside.write_bytes(b"")
            path.symlink_to(outside)
            with self.assertRaises(OSError):
                gate.acquire(root)
            path.unlink()
            os.link(outside, path)
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                gate.acquire(root)
            path.unlink()
            path.write_bytes(b"")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                gate.acquire(root)

    def test_missing_root_and_symlink_component_are_not_implicitly_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                gate.acquire(root / "missing", create=False)
            (root / "alias").symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "real directories"):
                gate.acquire(root / "alias", create=False)
