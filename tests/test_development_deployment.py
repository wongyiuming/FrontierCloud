import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DevelopmentDeploymentTests(unittest.TestCase):
    def test_environment_is_never_hardcoded_by_compose(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

        self.assertNotIn("ENVIRONMENT: production", compose)
        self.assertIn("ENVIRONMENT: ${ENVIRONMENT:-production}", compose)
        self.assertIn("ENVIRONMENT=${ENVIRONMENT:-production}", compose)

    def test_development_nginx_is_http_only(self):
        development = ROOT / "nginx" / "environments" / "development"
        rendered_inputs = "\n".join(
            path.read_text(encoding="utf-8") for path in development.glob("*.conf")
        )

        self.assertIn("listen 80 default_server", rendered_inputs)
        self.assertNotIn("listen 443", rendered_inputs)
        self.assertNotIn("ssl_certificate", rendered_inputs)

    def test_production_nginx_keeps_tls_and_redirect(self):
        production = ROOT / "nginx" / "environments" / "production"
        rendered_inputs = "\n".join(
            path.read_text(encoding="utf-8") for path in production.glob("*.conf")
        )

        self.assertIn("listen 443 ssl", rendered_inputs)
        self.assertIn("ssl_certificate /etc/nginx/certs/fullchain.pem", rendered_inputs)
        self.assertIn("return 301 https://$host$request_uri", rendered_inputs)


if __name__ == "__main__":
    unittest.main()
