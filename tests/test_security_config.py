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

    def test_web_container_runs_with_reduced_filesystem_and_process_privileges(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        compose = Path("docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("read_only: true", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("cap_drop:", compose)
        self.assertIn("./app:/app/app:ro", compose)
        self.assertIn("./static:/app/static:ro", compose)


if __name__ == "__main__":
    unittest.main()
