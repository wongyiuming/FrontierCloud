import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DevelopmentDeploymentTests(unittest.TestCase):
    @staticmethod
    def _service_block(compose: str, service: str) -> str:
        match = re.search(
            rf"(?ms)^  {service}:\n(.*?)(?=^  [a-zA-Z][a-zA-Z0-9_]*:\n|\Z)",
            compose,
        )
        if match is None:
            raise AssertionError(f"missing Compose service: {service}")
        return match.group(1)

    def test_environment_is_never_hardcoded_by_compose(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

        self.assertNotIn("ENVIRONMENT: production", compose)
        self.assertIn("ENVIRONMENT: ${ENVIRONMENT:-production}", compose)
        self.assertIn("ENVIRONMENT=${ENVIRONMENT:-production}", compose)

    def test_admin_bootstrap_token_is_validated_before_startup(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        web = self._service_block(compose, "web")

        self.assertIn(
            "ADMIN_BOOTSTRAP_TOKEN: "
            "${ADMIN_BOOTSTRAP_TOKEN:?ADMIN_BOOTSTRAP_TOKEN must be set}",
            web,
        )

    def test_upload_inactivity_timeout_is_consistent_across_app_and_proxy(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        nginx = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        config = (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn(
            "UPLOAD_INACTIVITY_TIMEOUT=${ADMIN_UPLOAD_INACTIVITY_TIMEOUT:-300}",
            compose,
        )
        self.assertIn("client_body_timeout ${UPLOAD_INACTIVITY_TIMEOUT}s", nginx)
        self.assertIn("proxy_send_timeout ${UPLOAD_INACTIVITY_TIMEOUT}s", nginx)
        self.assertIn("ADMIN_UPLOAD_INACTIVITY_TIMEOUT: int = Field(", config)
        self.assertIn("# ADMIN_UPLOAD_INACTIVITY_TIMEOUT=300", env_example)

    def test_environment_example_keeps_defaults_as_optional_overrides(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        active_names = {
            line.split("=", 1)[0]
            for line in env_example.splitlines()
            if line and not line.startswith("#") and "=" in line
        }

        self.assertEqual(
            active_names,
            {
                "ENVIRONMENT",
                "SERVER_NAME",
                "ADMIN_BOOTSTRAP_TOKEN",
                "MYSQL_PASSWORD",
                "MYSQL_ROOT_PASSWORD",
                "INTERNAL_METRICS_TOKEN",
                "MONITORING_ALLOW_CIDR",
                "METRICS_BASIC_PASSWORD",
                "MYSQL_EXPORTER_PASSWORD",
                "MYSQL_BACKUP_PASSWORD",
            },
        )
        self.assertIn("# REDIS_URL=redis://redis:6379/0", env_example)
        self.assertIn("# HTTP_PORT=80", env_example)

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
        self.assertIn("/.well-known/acme-challenge/", rendered_inputs)

    def test_test_environment_uses_production_like_nginx(self):
        selector = (ROOT / "nginx" / "15-select-environment.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("test|production) nginx_mode=production", selector)
        self.assertNotIn("development|test) nginx_mode=development", selector)

    def test_collection_agents_run_without_optional_profiles(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        for service in (
            "node_exporter",
            "cadvisor",
            "redis_exporter",
            "mysql_exporter",
            "nginx_exporter",
            "nginxlog_exporter",
            "nginxlog_limiter",
        ):
            self.assertNotIn("profiles:", self._service_block(compose, service))

        self.assertIn(
            'profiles: ["monitoring"]', self._service_block(compose, "mysql_backup")
        )
        self.assertIn(":/backups:z", self._service_block(compose, "mysql_backup"))
        mysql_init = self._service_block(compose, "mysql_monitoring_init")
        self.assertIn("mysql_socket:/var/run/mysqld", mysql_init)
        self.assertIn("condition: service_healthy", mysql_init)

    def test_business_image_excludes_standalone_download_tools(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "docker.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("COPY auto_download", dockerfile)
        self.assertNotIn("/app/auto_download", dockerfile)
        self.assertIn("auto_download", dockerignore.splitlines())
        self.assertNotIn("test_media_sync.py", workflow)
        self.assertNotIn("test_download_support.py", workflow)

    def test_office_document_processing_is_not_shipped(self):
        endpoints = (ROOT / "app" / "api" / "v1" / "endpoints.py").read_text(
            encoding="utf-8"
        )
        dependencies = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        nginx = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")

        self.assertNotIn("/watermark", endpoints)
        self.assertNotIn("/watermark", nginx)
        for dependency in ("Pillow", "pymupdf", "python-docx", "py7zr"):
            self.assertNotIn(dependency, dependencies)
        self.assertNotIn("WATERMARK_FONT_PATH", dockerfile)

    def test_ci_runs_source_only_deployment_tests_on_the_host(self):
        workflow = (ROOT / ".github" / "workflows" / "docker.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(workflow.count("tests/test_development_deployment.py"), 1)
        self.assertIn("! -name 'test_development_deployment.py'", workflow)
        self.assertIn("ENVIRONMENT=test", workflow)
        self.assertIn("python3 scripts/check_english_comments.py", workflow)
        self.assertIn("monitoring/reporting/Dockerfile", workflow)
        self.assertIn("Test production-like collection agents", workflow)
        self.assertIn("frontiercloud_backup_last_run_success 1", workflow)

    def test_cd_can_only_deploy_a_successful_dev_push_to_rn(self):
        workflow = (ROOT / ".github" / "workflows" / "docker.yml").read_text(
            encoding="utf-8"
        )
        deploy = workflow.split("  deploy-rn:", 1)[1]

        self.assertIn("needs: test-compose", deploy)
        self.assertIn("github.event_name == 'push'", deploy)
        self.assertIn("github.ref == 'refs/heads/dev'", deploy)
        self.assertNotIn("refs/heads/main", deploy)
        self.assertIn("runs-on: [self-hosted, Linux, X64, rn-preproduction]", deploy)
        self.assertIn("name: rn-preproduction", deploy)
        self.assertIn('test "$GITHUB_REF" = refs/heads/dev', deploy)
        self.assertIn('test "$(git rev-parse origin/dev)" = "$GITHUB_SHA"', deploy)
        self.assertNotIn("workflow_dispatch", workflow)


if __name__ == "__main__":
    unittest.main()
