import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import runtime_secrets


class RuntimeSecretsTests(unittest.TestCase):
    def test_init_generates_strong_values_once_and_does_not_rotate_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "mysql_password", root / "mysql_root_password", root / "admin_key"]
            marker = root / ".announce-once"
            with patch.object(runtime_secrets, "SECRET_DIR", root), \
                 patch.object(runtime_secrets, "MYSQL_PASSWORD_FILE", paths[0]), \
                 patch.object(runtime_secrets, "MYSQL_ROOT_PASSWORD_FILE", paths[1]), \
                 patch.object(runtime_secrets, "ADMIN_KEY_FILE", paths[2]), \
                 patch.object(runtime_secrets, "ANNOUNCE_MARKER", marker), \
                 patch.object(runtime_secrets, "_set_web_owner"):
                with patch.dict(runtime_secrets.os.environ, {}, clear=True):
                    runtime_secrets.initialize_runtime_secrets()
                first = [path.read_text(encoding="utf-8").strip() for path in paths]
                with patch.dict(runtime_secrets.os.environ, {}, clear=True):
                    runtime_secrets.initialize_runtime_secrets()
                second = [path.read_text(encoding="utf-8").strip() for path in paths]
            self.assertEqual(first, second)
            self.assertEqual(len(set(first)), 3)
            self.assertTrue(all(len(value) >= 48 for value in first))
            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
