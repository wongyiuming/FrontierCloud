import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExporterConfigurationTests(unittest.TestCase):
    def test_exporters_are_internal_only_and_resource_bounded(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        for service in (
            "node_exporter",
            "cadvisor",
            "redis_exporter",
            "mysql_exporter",
            "nginx_exporter",
            "nginxlog_exporter",
        ):
            match = re.search(
                rf"(?ms)^  {service}:\n(.*?)(?=^  [a-zA-Z][a-zA-Z0-9_]*:\n|\Z)",
                compose,
            )
            self.assertIsNotNone(match)
            block = match.group(1)
            self.assertNotIn("ports:", block)
            self.assertIn("mem_limit:", block)
            self.assertIn("max-size:", block if service == "node_exporter" else compose)

    def test_allowlist_and_secrets_are_environment_substitutions(self):
        nginx = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("allow ${MONITORING_ALLOW_CIDR};", nginx)
        self.assertIn("auth_basic_user_file", nginx)
        self.assertIn('${INTERNAL_METRICS_TOKEN}', nginx)
        self.assertIsNone(re.search(r"allow\s+(?:\d{1,3}\.){3}\d{1,3}", nginx))

    def test_nginx_log_metrics_include_status_and_latency(self):
        nginx = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        exporter = (ROOT / "monitoring" / "nginxlog-exporter.hcl").read_text(encoding="utf-8")
        self.assertIn("$status", nginx)
        self.assertIn("$request_time", nginx)
        self.assertIn("$upstream_response_time", exporter)


if __name__ == "__main__":
    unittest.main()
