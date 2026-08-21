import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"


class MonitoringStackTests(unittest.TestCase):
    def test_management_ports_only_bind_loopback_and_resources_are_bounded(self):
        compose = (MONITORING / "docker-compose.yaml").read_text(encoding="utf-8")
        expected_limits = {
            "prometheus": "768m",
            "grafana": "384m",
            "alertmanager": "128m",
            "blackbox": "128m",
        }
        for service, limit in expected_limits.items():
            match = re.search(
                rf"(?ms)^  {service}:\n(.*?)(?=^  [a-zA-Z][a-zA-Z0-9_]*:\n|\Z)",
                compose,
            )
            self.assertIsNotNone(match)
            block = match.group(1)
            self.assertIn(f"mem_limit: {limit}", block)
            for binding in re.findall(r"- (\S+:\d+:\d+)", block):
                self.assertTrue(binding.startswith("127.0.0.1:"))

    def test_prometheus_retention_is_time_and_size_bounded(self):
        compose = (MONITORING / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("--storage.tsdb.retention.time=24h", compose)
        self.assertIn("--storage.tsdb.retention.size=6GB", compose)

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
