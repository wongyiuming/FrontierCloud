import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"


class MonitoringStackTests(unittest.TestCase):
    def test_gateway_uses_dedicated_ports_and_resources_are_bounded(self):
        compose = (MONITORING / "docker-compose.yaml").read_text(encoding="utf-8")
        expected_limits = {
            "prometheus": "768m",
            "grafana": "384m",
            "alertmanager": "128m",
            "blackbox": "128m",
            "gateway": "64m",
        }
        for service, limit in expected_limits.items():
            match = re.search(
                rf"(?ms)^  {service}:\n(.*?)(?=^  [a-zA-Z][a-zA-Z0-9_]*:\n|\Z)",
                compose,
            )
            self.assertIsNotNone(match)
            block = match.group(1)
            self.assertIn(f"mem_limit: {limit}", block)
            if service != "gateway":
                self.assertNotIn("ports:", block)
        self.assertIn('"${MONITORING_HTTP_PORT:-8080}:8080"', compose)
        self.assertIn('"${MONITORING_HTTPS_PORT:-8443}:8443"', compose)
        self.assertNotIn('"80:8080"', compose)
        self.assertNotIn('"443:8443"', compose)

    def test_prometheus_retention_is_time_and_size_bounded(self):
        compose = (MONITORING / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("--storage.tsdb.retention.time=24h", compose)
        self.assertIn("--storage.tsdb.retention.size=6GB", compose)

    def test_management_services_use_native_authentication(self):
        compose = (MONITORING / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertEqual(compose.count("--web.config.file="), 2)
        self.assertIn("GF_AUTH_ANONYMOUS_ENABLED: \"false\"", compose)
        self.assertIn("GF_AUTH_BASIC_PASSWORD_POLICY: \"true\"", compose)
        template = (MONITORING / "templates" / "web.yml.template").read_text(encoding="utf-8")
        self.assertIn("basic_auth_users:", template)

    def test_prometheus_self_scrape_uses_the_external_route_prefix(self):
        template = (MONITORING / "templates" / "prometheus.yml.template").read_text(encoding="utf-8")
        self.assertIn("metrics_path: /prometheus/metrics", template)

    def test_gateway_uses_https_subpaths_and_security_headers(self):
        config = (MONITORING / "nginx" / "nginx.conf.template").read_text(encoding="utf-8")
        self.assertIn("location /grafana/", config)
        self.assertIn("location /prometheus/", config)
        self.assertIn("location /alertmanager/", config)
        self.assertIn("Strict-Transport-Security", config)
        self.assertIn("/.well-known/acme-challenge/", config)
        self.assertIn("https://$host:__MONITORING_HTTPS_PORT__", config)

    def test_login_limiter_does_not_throttle_grafana_assets(self):
        config = (MONITORING / "nginx" / "nginx.conf.template").read_text(encoding="utf-8")
        self.assertIn("limit_req_status 429;", config)
        self.assertIn("location = /grafana/login {", config)
        grafana_location = config.split("location /grafana/ {", 1)[1].split("}", 1)[0]
        self.assertNotIn("limit_req", grafana_location)
        self.assertEqual(config.count("limit_req zone=monitoring_login"), 1)

    def test_renderer_uses_python39_compatible_file_writes(self):
        renderer = (MONITORING / "render_config.py").read_text(encoding="utf-8")
        self.assertNotIn(".write_text(", renderer)
        self.assertIn('.open("w", encoding="utf-8", newline="\\n")', renderer)

    def test_runtime_files_are_readable_only_by_service_users(self):
        renderer = (MONITORING / "render_config.py").read_text(encoding="utf-8")
        self.assertIn("PROMETHEUS_UID = 65534", renderer)
        self.assertIn("GRAFANA_UID = 472", renderer)
        self.assertIn("os.chown(temporary, uid, gid)", renderer)
        self.assertIn("os.chmod(temporary, 0o400)", renderer)

    def test_alertmanager_is_telegram_only_and_sends_resolved(self):
        template = (MONITORING / "templates" / "alertmanager.yml.template").read_text(encoding="utf-8")
        self.assertIn("telegram_configs:", template)
        self.assertIn("bot_token_file:", template)
        self.assertIn("send_resolved: true", template)
        self.assertNotIn("email_configs", template)
        self.assertIn("repeat_interval: 4h", template)

    def test_alert_rules_have_sustained_thresholds_and_recovery_capability(self):
        rules = (MONITORING / "prometheus" / "alerts.yml").read_text(encoding="utf-8")
        alert_count = len(re.findall(r"^\s+- alert:", rules, re.MULTILINE))
        duration_count = len(re.findall(r"^\s+for:", rules, re.MULTILINE))
        self.assertEqual(alert_count, duration_count)
        self.assertIn("alert: HostDiskLow", rules)
        self.assertIn("alert: FrontierCloudHealthDown", rules)

    def test_grafana_dashboard_covers_core_services(self):
        dashboard = json.loads((MONITORING / "grafana" / "dashboards" / "frontiercloud-overview.json").read_text(encoding="utf-8"))
        titles = {panel["title"] for panel in dashboard["panels"]}
        self.assertTrue({"Host CPU %", "Host Memory %", "Redis Up", "MySQL Up", "Nginx Connections"} <= titles)
        self.assertTrue({"Backup Age", "Last Backup Size"} <= titles)

    def test_daily_backup_uses_environment_password_and_publishes_history_metrics(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        script = (MONITORING / "mysql_backup.sh").read_text(encoding="utf-8")
        rules = (MONITORING / "prometheus" / "alerts.yml").read_text(encoding="utf-8")
        self.assertIn("MYSQL_PWD:", compose)
        self.assertIn("MYSQL_BACKUP_PASSWORD", compose)
        self.assertIn("MYSQL_BACKUP_USER", compose)
        self.assertNotIn("--password=", script)
        self.assertNotIn("--user=root", script)
        self.assertIn('--user="$MYSQL_BACKUP_USER"', script)
        self.assertIn("--single-transaction", script)
        self.assertIn("frontiercloud_backup_last_success_timestamp_seconds", script)
        self.assertIn("-mtime +7 -delete", script)
        self.assertIn("alert: MySQLBackupStale", rules)


if __name__ == "__main__":
    unittest.main()
