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
            initializing = root / ".initializing"
            initialized = root / ".initialized"
            with patch.object(runtime_secrets, "SECRET_DIR", root), \
                 patch.object(runtime_secrets, "MYSQL_PASSWORD_FILE", paths[0]), \
                 patch.object(runtime_secrets, "MYSQL_ROOT_PASSWORD_FILE", paths[1]), \
                 patch.object(runtime_secrets, "ADMIN_KEY_FILE", paths[2]), \
                 patch.object(runtime_secrets, "ANNOUNCE_MARKER", marker), \
                 patch.object(runtime_secrets, "INITIALIZING_MARKER", initializing), \
                 patch.object(runtime_secrets, "INITIALIZED_MARKER", initialized), \
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
            self.assertTrue(initialized.exists())
            self.assertFalse(initializing.exists())

    def test_interrupted_initialization_with_complete_files_still_announces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "mysql_password", root / "mysql_root_password", root / "admin_key"]
            values = ["mysql-secret", "root-secret", "admin-secret"]
            for path, value in zip(paths, values, strict=True):
                path.write_text(value + "\n", encoding="utf-8")
            initializing = root / ".initializing"
            initializing.write_text("1\n", encoding="utf-8")
            marker = root / ".announce-once"
            initialized = root / ".initialized"
            with patch.object(runtime_secrets, "SECRET_DIR", root), \
                 patch.object(runtime_secrets, "MYSQL_PASSWORD_FILE", paths[0]), \
                 patch.object(runtime_secrets, "MYSQL_ROOT_PASSWORD_FILE", paths[1]), \
                 patch.object(runtime_secrets, "ADMIN_KEY_FILE", paths[2]), \
                 patch.object(runtime_secrets, "ANNOUNCE_MARKER", marker), \
                 patch.object(runtime_secrets, "INITIALIZING_MARKER", initializing), \
                 patch.object(runtime_secrets, "INITIALIZED_MARKER", initialized), \
                 patch.object(runtime_secrets, "_set_web_owner"):
                runtime_secrets.initialize_runtime_secrets()

            self.assertEqual(
                [path.read_text(encoding="utf-8").strip() for path in paths],
                values,
            )
            self.assertTrue(marker.exists())
            self.assertTrue(initialized.exists())
            self.assertFalse(initializing.exists())

    def test_legacy_complete_secrets_are_not_reannounced_on_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "mysql_password", root / "mysql_root_password", root / "admin_key"]
            for path in paths:
                path.write_text("existing-secret\n", encoding="utf-8")
            marker = root / ".announce-once"
            initializing = root / ".initializing"
            initialized = root / ".initialized"
            with patch.object(runtime_secrets, "SECRET_DIR", root), \
                 patch.object(runtime_secrets, "MYSQL_PASSWORD_FILE", paths[0]), \
                 patch.object(runtime_secrets, "MYSQL_ROOT_PASSWORD_FILE", paths[1]), \
                 patch.object(runtime_secrets, "ADMIN_KEY_FILE", paths[2]), \
                 patch.object(runtime_secrets, "ANNOUNCE_MARKER", marker), \
                 patch.object(runtime_secrets, "INITIALIZING_MARKER", initializing), \
                 patch.object(runtime_secrets, "INITIALIZED_MARKER", initialized), \
                 patch.object(runtime_secrets, "_set_web_owner"):
                runtime_secrets.initialize_runtime_secrets()

            self.assertFalse(marker.exists())
            self.assertTrue(initialized.exists())


if __name__ == "__main__":
    unittest.main()
