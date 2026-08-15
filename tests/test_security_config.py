import os
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.core.config import Settings


class ProductionConfigurationTests(unittest.TestCase):
    def test_production_rejects_known_weak_secrets_and_insecure_cookie(self):
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                ENVIRONMENT="production",
                REDIS_URL="redis://redis:6379/0",
                WALL_ADMIN_TOKEN="short-token",
                MYSQL_URL="mysql+asyncmy://media_admin:Huawei%40123@mysql/db",
                MYSQL_PASSWORD="Huawei@123",
                MYSQL_ROOT_PASSWORD="Huawei@123",
                ADMIN_COOKIE_SECURE=False,
            )

    def test_production_accepts_independent_strong_secrets(self):
        password = "database-secret-123456789"
        settings = Settings(
            _env_file=None,
            ENVIRONMENT="production",
            REDIS_URL="redis://redis:6379/0",
            WALL_ADMIN_TOKEN="wall-token-0123456789abcdef0123456789abcdef",
            MYSQL_URL=f"mysql+asyncmy://media_admin:{password}@mysql/db",
            MYSQL_PASSWORD=password,
            MYSQL_ROOT_PASSWORD="different-root-secret-987654321",
            ADMIN_COOKIE_SECURE=True,
        )
        self.assertEqual(settings.ENVIRONMENT, "production")

    def test_token_issue_interval_cannot_create_a_lifecycle_gap(self):
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                ENVIRONMENT="test",
                REDIS_URL="redis://redis:6379/0",
                WALL_ADMIN_TOKEN="test-token",
                MYSQL_URL="mysql+asyncmy://media_admin:test@mysql/db",
                MYSQL_PASSWORD="test",
                MYSQL_ROOT_PASSWORD="test",
                ADMIN_TOKEN_TTL=900,
                ADMIN_TOKEN_ISSUE_INTERVAL=901,
            )

    def test_web_container_runs_with_reduced_filesystem_and_process_privileges(self):
        if os.name != "posix" or not Path("/.dockerenv").exists():
            self.skipTest("container runtime hardening is verified inside Docker")

        self.assertEqual(os.geteuid(), 10001)
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
