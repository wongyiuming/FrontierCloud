import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentContractTests(unittest.TestCase):
    def test_runtime_dependencies_are_reproducibly_pinned(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        unpinned = [dependency for dependency in project["dependencies"] if "==" not in dependency]
        self.assertEqual(unpinned, [])

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
        self.assertEqual(active, set())
        self.assertEqual(len(listed), len(set(listed)))
        lines = env_example.splitlines()
        for index, line in enumerate(lines):
            if re.match(r"^# [A-Z][A-Z0-9_]*=", line):
                self.assertGreater(index, 0)
                self.assertTrue(lines[index - 1].startswith("# "))
                self.assertNotRegex(lines[index - 1], r"^# [A-Z][A-Z0-9_]*=")
        app_names = set(re.findall(r'validation_alias="([A-Z][A-Z0-9_]*)"', config))
        compose_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose))
        self.assertEqual(set(listed), app_names | compose_names)

    def test_private_deployment_variables_are_absent_from_contract(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for name in (
            "PROMETHEUS_URL", "GRAFANA_URL", "ELASTICSEARCH_URL", "LOGSTASH_HOST",
            "KIBANA_URL", "RN_HOST", "DMIT_HOST", "WG_ENDPOINT",
            "PRODUCTION_HOST", "STAGING_HOST", "ADMIN_BOOTSTRAP_TOKEN",
            "METRICS_TOKEN",
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

    def test_transport_and_upload_timeout_are_direct_technical_settings(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertNotIn("ENVIRONMENT", compose)
        self.assertIn("TLS_ENABLED: ${TLS_ENABLED:-false}", compose)
        self.assertIn("UPLOAD_INACTIVITY_TIMEOUT: ${ADMIN_UPLOAD_INACTIVITY_TIMEOUT:-300}", compose)
        self.assertIn("client_body_timeout ${UPLOAD_INACTIVITY_TIMEOUT}s", nginx)

    def test_nginx_selects_transport_without_deployment_tiers(self):
        selector = (ROOT / "nginx/15-select-tls.sh").read_text(encoding="utf-8")
        self.assertIn('${TLS_ENABLED:-false}', selector)
        self.assertIn("nginx_mode=https", selector)
        self.assertIn("nginx_mode=http", selector)
        self.assertIn('/etc/nginx/transport/$nginx_mode', selector)
        for transport in ("http", "https"):
            for name in ("extra-servers.conf", "public-listen.conf", "public-tls.conf"):
                self.assertTrue((ROOT / "nginx/transport" / transport / name).is_file())
        http = (ROOT / "nginx/transport/http/public-listen.conf").read_text(encoding="utf-8")
        https = (ROOT / "nginx/transport/https/public-listen.conf").read_text(encoding="utf-8")
        self.assertIn("listen 80 default_server", http)
        self.assertIn("listen 443 ssl", https)
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertIn("/etc/nginx/runtime/extra-servers.conf", nginx)

    def test_runtime_and_readme_do_not_define_deployment_tiers(self):
        paths = [ROOT / "README.md", ROOT / "app/core/config.py", ROOT / "app/api/v1/admin.py"]
        paths.extend(path for path in (ROOT / "nginx").rglob("*") if path.is_file())
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotRegex(
                    path.read_text(encoding="utf-8"),
                    r"(?i)\b(?:production|preproduction|staging|development)\b|(?:生产|预发布|测试|开发)环境",
                )

    def test_nginx_limits_large_bodies_to_upload_routes_and_sets_security_headers(self):
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        headers = (ROOT / "nginx/security-headers.conf").read_text(encoding="utf-8")
        dockerfile = (ROOT / "nginx/Dockerfile").read_text(encoding="utf-8")
        self.assertEqual(nginx.count("client_max_body_size 820M"), 1)
        self.assertIn("location /api/v1/media/admin/upload/", nginx)
        self.assertIn("client_max_body_size 64k", nginx)
        self.assertIn("X-Content-Type-Options", headers)
        self.assertIn("X-Frame-Options", headers)
        self.assertIn("Strict-Transport-Security", headers)
        self.assertIn("COPY nginx/security-headers.conf", dockerfile)

    def test_nginx_emits_structured_logs_without_a_log_directory(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertIn("access_log /dev/stdout structured if=$access_loggable", nginx)
        self.assertIn("~^/health(?:/|$) 0", nginx)
        self.assertIn("=/metrics 0", nginx)
        self.assertIn("error_log /dev/stderr warn", nginx)
        self.assertIn('"request_id"', nginx)
        self.assertIn('"trace_id"', nginx)
        self.assertNotIn("/var/log/nginx", nginx + compose)

    def test_duplicate_uvicorn_access_log_is_disabled(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        logging_config = (ROOT / "app/core/logging_config.py").read_text(encoding="utf-8")
        self.assertIn("--no-access-log", dockerfile)
        self.assertIn('logging.getLogger("uvicorn.access")', logging_config)
        self.assertIn("access_logger.disabled = True", logging_config)

    def test_runtime_secret_initializer_has_no_legacy_environment_inputs(self):
        initializer = (ROOT / "app/services/runtime_secrets.py").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        deploy = (ROOT / "scripts/deploy_rn.sh").read_text(encoding="utf-8")
        for obsolete in (
            "ADMIN_BOOTSTRAP_TOKEN", "MYSQL_PASSWORD=", "MYSQL_ROOT_PASSWORD=",
            "MYSQL_URL=", "WEBRTC_STUN_URLS=", "SECURITY_AUTO_BAN_TTL=",
            "METRICS_TOKEN=",
        ):
            self.assertNotIn(obsolete, initializer + compose + deploy)

    def test_env_contract_validator_rejects_unknown_names_without_printing_values(self):
        validator = ROOT / "scripts/validate_env_contract.py"
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            example = temp_root / ".env.example"
            env_file = temp_root / ".env"
            example.write_text("# Supported switch.\n# TLS_ENABLED=false\n", encoding="utf-8")
            env_file.write_text(
                "TLS_ENABLED=true\nENVIRONMENT=test\nMETRICS_BASIC_PASSWORD=do_not_print_me\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(validator), "--env-file", str(env_file),
                 "--example-file", str(example)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ENVIRONMENT", result.stdout)
        self.assertIn("METRICS_BASIC_PASSWORD", result.stdout)
        self.assertNotIn("do_not_print_me", result.stdout + result.stderr)

    def test_env_contract_validator_accepts_only_formal_variables(self):
        validator = ROOT / "scripts/validate_env_contract.py"
        result = subprocess.run(
            [sys.executable, str(validator), "--env-file", str(ROOT / ".env.example"),
             "--example-file", str(ROOT / ".env.example")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_readme_documents_runtime_secret_recovery_after_web_recreation(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docker compose logs web | grep initial_runtime_secrets", readme)
        self.assertIn(
            "docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'",
            readme,
        )
        self.assertIn(
            "docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'",
            readme,
        )
        self.assertIn("Container recreation can remove that log entry", readme)
        self.assertIn("persistent `runtime_secrets` volume", readme)
        self.assertIn("privilege-elevation control", readme)

    def test_cd_can_only_deploy_a_successful_dev_push_to_rn(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        deploy_script = (ROOT / "scripts/deploy_rn.sh").read_text(encoding="utf-8")
        deploy = workflow.split("  deploy-rn:", 1)[1]
        self.assertIn("needs: test-compose", deploy)
        self.assertIn("github.event_name == 'push'", deploy)
        self.assertIn("github.ref == 'refs/heads/dev'", deploy)
        self.assertNotIn("refs/heads/main", deploy)
        self.assertNotIn("workflow_dispatch", workflow)
        self.assertIn("python3 scripts/validate_env_contract.py", workflow)
        self.assertIn("python3 scripts/validate_env_contract.py", deploy_script)


if __name__ == "__main__":
    unittest.main()
