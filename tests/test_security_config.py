import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.core.config import Settings


class SecurityConfigurationTests(unittest.TestCase):
    def test_weak_secrets_are_rejected_in_every_transport_mode(self):
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                TLS_ENABLED=False,
                REDIS_URL="redis://redis:6379/0",
                MYSQL_URL="mysql+asyncmy://media_admin:Huawei%40123@mysql/db",
                MYSQL_PASSWORD="Huawei@123",
                MYSQL_ROOT_PASSWORD="Huawei@123",
            )

    def test_transport_switch_derives_safe_cookie_behavior(self):
        password = "database-secret-123456789"
        secure = Settings(
            _env_file=None,
            TLS_ENABLED=True,
            REDIS_URL="redis://redis:6379/0",
            MYSQL_URL=f"mysql+asyncmy://media_admin:{password}@mysql/db",
            MYSQL_PASSWORD=password,
            MYSQL_ROOT_PASSWORD="different-root-secret-987654321",
        )
        plain = secure.model_copy(update={"TLS_ENABLED": False})

        self.assertTrue(secure.ADMIN_COOKIE_SECURE)
        self.assertEqual(secure.ADMIN_COOKIE_NAME, "__Host-admin_session")
        self.assertEqual(secure.ADMIN_CSRF_COOKIE_NAME, "__Host-admin-csrf")
        self.assertFalse(plain.ADMIN_COOKIE_SECURE)
        self.assertEqual(plain.ADMIN_COOKIE_NAME, "admin_session")
        self.assertEqual(plain.ADMIN_CSRF_COOKIE_NAME, "admin_csrf")

    def test_internal_urls_are_derived_from_minimal_deployment_values(self):
        password = "database secret+with@reserved/chars"
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(
                _env_file=None,
                TLS_ENABLED=False,
                SERVER_NAME="preproduction.example.com",
                WEBRTC_STUN_PORT=5349,
                MYSQL_PASSWORD=password,
                MYSQL_ROOT_PASSWORD="test-root-password-0123456789",
            )

        self.assertEqual(settings.REDIS_URL, "redis://redis:6379/0")
        self.assertEqual(
            settings.MYSQL_URL,
            "mysql+asyncmy://media_admin:database%20secret%2Bwith%40reserved%2Fchars@mysql:3306/office_automation",
        )
        self.assertEqual(
            settings.WEBRTC_STUN_URLS,
            "stun:preproduction.example.com:5349",
        )

    def test_token_issue_interval_cannot_create_a_lifecycle_gap(self):
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                REDIS_URL="redis://redis:6379/0",
                MYSQL_URL="mysql+asyncmy://media_admin:test-password-012345@mysql/db",
                MYSQL_PASSWORD="test-password-012345",
                MYSQL_ROOT_PASSWORD="test-root-password-012345",
                ADMIN_TOKEN_TTL=900,
                ADMIN_TOKEN_ISSUE_INTERVAL=901,
            )

    def test_web_container_runs_with_reduced_filesystem_and_process_privileges(self):
        if os.name != "posix" or not Path("/.dockerenv").exists():
            self.skipTest("container runtime hardening is verified inside Docker")

        self.assertEqual(os.geteuid(), 10001)
        self.assertTrue(os.access(Path("/app/main.py"), os.R_OK))
        self.assertTrue(os.access(Path("/app/app/api/v1/endpoints.py"), os.R_OK))
        self.assertTrue(os.access(Path("/app/static/media/index.html"), os.R_OK))
        process_status = Path("/proc/self/status").read_text(encoding="utf-8")
        status_fields = dict(
            line.split(":", 1) for line in process_status.splitlines() if ":" in line
        )
        self.assertEqual(status_fields["NoNewPrivs"].strip(), "1")
        self.assertEqual(int(status_fields["CapEff"].strip(), 16), 0)

        for directory in (Path("/app"), Path("/app/app"), Path("/app/static")):
            probe = directory / ".container-write-probe"
            try:
                probe.write_text("must not be writable", encoding="utf-8")
            except OSError:
                continue
            else:
                probe.unlink(missing_ok=True)
                self.fail(f"container path is unexpectedly writable: {directory}")


if __name__ == "__main__":
    unittest.main()
