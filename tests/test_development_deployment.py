import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DevelopmentDeploymentTests(unittest.TestCase):
    def test_environment_example_is_the_complete_public_contract(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        config = (ROOT / "app/core/config.py").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        listed = re.findall(r"(?m)^#? ?([A-Z][A-Z0-9_]*)=", env_example)
        active = {
            line.split("=", 1)[0]
            for line in env_example.splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        self.assertEqual(active, {"MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD"})
        self.assertEqual(len(listed), len(set(listed)))
        app_names = set(re.findall(r'validation_alias="([A-Z][A-Z0-9_]*)"', config))
        compose_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose))
        self.assertEqual(set(listed), app_names | compose_names)

    def test_private_deployment_variables_are_absent_from_contract(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for name in (
            "PROMETHEUS_URL", "GRAFANA_URL", "ELASTICSEARCH_URL", "LOGSTASH_HOST",
            "KIBANA_URL", "RN_HOST", "DMIT_HOST", "WG_ENDPOINT",
            "PRODUCTION_HOST", "STAGING_HOST", "ADMIN_BOOTSTRAP_TOKEN",
        ):
            self.assertNotIn(f"{name}=", env_example)

    def test_observability_platform_lifecycle_is_not_bundled(self):
        self.assertFalse(any(
            path.is_file() and path.suffix != ".pyc"
            for path in (ROOT / "monitoring").rglob("*")
        ))
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8").lower()
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8").lower()
        for marker in (
            "prometheus", "grafana", "elasticsearch", "logstash", "kibana",
            "node_exporter", "cadvisor", "redis_exporter", "mysql_exporter",
            "nginx_exporter", "monitoring", "weekly_reporter",
        ):
            self.assertNotIn(marker, compose)
            self.assertNotIn(marker, workflow)

    def test_download_tools_are_scripts_not_runtime_packages(self):
        self.assertFalse(any(
            path.is_file() and path.suffix != ".pyc"
            for path in (ROOT / "auto_download").rglob("*")
        ))
        self.assertTrue((ROOT / "scripts/auto_download").is_dir())
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("scripts/auto_download", dockerignore)
        self.assertNotIn('"auto_download*"', pyproject)

    def test_transport_and_upload_timeout_are_direct_technical_settings(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertNotIn("ENVIRONMENT", compose)
        self.assertIn("TLS_ENABLED: ${TLS_ENABLED:-true}", compose)
        self.assertIn("UPLOAD_INACTIVITY_TIMEOUT: ${ADMIN_UPLOAD_INACTIVITY_TIMEOUT:-300}", compose)
        self.assertIn("client_body_timeout ${UPLOAD_INACTIVITY_TIMEOUT}s", nginx)

    def test_cd_can_only_deploy_a_successful_dev_push_to_rn(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        deploy = workflow.split("  deploy-rn:", 1)[1]
        self.assertIn("needs: test-compose", deploy)
        self.assertIn("github.event_name == 'push'", deploy)
        self.assertIn("github.ref == 'refs/heads/dev'", deploy)
        self.assertNotIn("refs/heads/main", deploy)
        self.assertNotIn("workflow_dispatch", workflow)


if __name__ == "__main__":
    unittest.main()
