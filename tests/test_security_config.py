import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.core import config
from app.core.config import Settings


class SecurityConfigurationTests(unittest.TestCase):
    def test_http_has_zero_required_settings(self):
        settings = Settings(_env_file=None)
        self.assertFalse(settings.TLS_ENABLED)
        self.assertEqual(settings.SERVER_NAME, "localhost")
        self.assertEqual(settings.WEBRTC_REPORT_COOLDOWN, 30)

    def test_https_requires_public_server_name(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, TLS_ENABLED=True)
        settings = Settings(_env_file=None, TLS_ENABLED=True, SERVER_NAME="media.example.com")
        self.assertTrue(settings.ADMIN_COOKIE_SECURE)
        self.assertEqual(settings.ADMIN_COOKIE_NAME, "__Host-admin_session")

    def test_mysql_url_reads_initialized_secret_file(self):
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "mysql_password"
            password_file.write_text("database secret+with@reserved/chars\n", encoding="utf-8")
            with patch.object(config, "MYSQL_PASSWORD_FILE", password_file):
                settings = Settings(_env_file=None)
        self.assertIn("database%20secret%2Bwith%40reserved%2Fchars", settings.MYSQL_URL)

    def test_webrtc_url_is_always_derived_from_server_name_and_port(self):
        settings = Settings(_env_file=None, SERVER_NAME="preproduction.example.com", WEBRTC_STUN_PORT=5349)
        self.assertEqual(settings.webrtc_stun_urls(), ["stun:preproduction.example.com:5349"])

    def test_log_contract_is_validated(self):
        self.assertEqual(Settings(_env_file=None, LOG_LEVEL="warning").LOG_LEVEL, "WARNING")
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, LOG_FORMAT="xml")


if __name__ == "__main__":
    unittest.main()
