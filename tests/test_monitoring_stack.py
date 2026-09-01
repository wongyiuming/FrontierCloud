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
            "prometheus": "${PROMETHEUS_MEMORY_LIMIT:-768m}",
            "grafana": "${GRAFANA_MEMORY_LIMIT:-384m}",
            "alertmanager": "128m",
            "blackbox": "128m",
            "gateway": "64m",
            "weekly_reporter": "128m",
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
        self.assertIn('"${MONITORING_BIND_ADDRESS:-127.0.0.1}:${MONITORING_HTTP_PORT:-8080}:8080"', compose)
        self.assertIn('"${MONITORING_BIND_ADDRESS:-127.0.0.1}:${MONITORING_HTTPS_PORT:-8443}:8443"', compose)
        self.assertNotIn('"80:8080"', compose)
        self.assertNotIn('"443:8443"', compose)

    def test_prometheus_retention_is_time_and_size_bounded(self):
        compose = (MONITORING / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("--storage.tsdb.retention.time=${PROMETHEUS_RETENTION_TIME:-8d}", compose)
        self.assertIn("--storage.tsdb.retention.size=${PROMETHEUS_RETENTION_SIZE:-3GB}", compose)

    def test_rn_cd_does_not_deploy_a_monitoring_compose_project(self):
        compose = (MONITORING / "docker-compose.yaml").read_text(encoding="utf-8")
        deploy = (ROOT / "scripts" / "deploy_rn.sh").read_text(encoding="utf-8")
        self_env = (MONITORING / "rn-self.env.example").read_text(encoding="utf-8")

        self.assertNotIn("container_name:", compose)
        self.assertNotIn("-p monitoring", deploy)
        self.assertNotIn("-p frontiercloud-rn-self-monitoring", deploy)
        self.assertIn("MONITORING_RUNTIME_DIR=./instances/rn-self", self_env)
        self.assertIn("MONITORING_EXTRA_CONFIG_DIR=./instances/rn-self/server.d", self_env)
        self.assertIn("MONITORING_HTTPS_PORT=9443", self_env)
        self.assertIn("PROMETHEUS_RETENTION_SIZE=1GB", self_env)

    def test_weekly_reporter_is_available_but_not_started_by_rn_cd(self):
        compose = (MONITORING / "docker-compose.yaml").read_text(encoding="utf-8")
        deploy = (ROOT / "scripts" / "deploy_rn.sh").read_text(encoding="utf-8")
        reporter = (MONITORING / "reporting" / "weekly_report.py").read_text(encoding="utf-8")

        self.assertIn('profiles: ["reporting"]', compose)
        self.assertNotIn("COMPOSE_PROFILES=reporting", deploy)
        self.assertIn("dbip-city-lite-", reporter)
        self.assertIn("IP Geolocation by DB-IP", reporter)
        self.assertIn("time.sleep(60)", reporter)
        self.assertIn("weekly_security_summary", (ROOT / "app" / "services" / "ip_security.py").read_text(encoding="utf-8"))
        compile(reporter, "weekly_report.py", "exec")

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
        self.assertIn("location /monitoring/", config)
        self.assertIn("location /elk/", config)
        self.assertIn('auth_basic "FrontierCloud ELK"', config)
        self.assertIn("location /prometheus/", config)
        self.assertIn("location /alertmanager/", config)
        self.assertIn("Strict-Transport-Security", config)
        self.assertIn("/.well-known/acme-challenge/", config)
        self.assertIn("https://$host:__MONITORING_HTTPS_PORT__", config)
        self.assertIn("include /etc/nginx/server.d/*.conf;", config)

    def test_login_limiter_does_not_throttle_grafana_assets(self):
        config = (MONITORING / "nginx" / "nginx.conf.template").read_text(encoding="utf-8")
        self.assertIn("limit_req_status 429;", config)
        self.assertIn("location = /monitoring/login {", config)
        grafana_location = config.split("location /monitoring/ {", 1)[1].split("}", 1)[0]
        self.assertNotIn("limit_req", grafana_location)
        self.assertEqual(config.count("limit_req zone=monitoring_login"), 1)

    def test_one_stack_separates_production_and_preproduction_by_labels(self):
        prometheus = (MONITORING / "templates" / "prometheus.yml.template").read_text(
            encoding="utf-8"
        )
        alertmanager = (MONITORING / "templates" / "alertmanager.yml.template").read_text(
            encoding="utf-8"
        )
        self.assertIn("__PRODUCTION_METRICS_HOST__", prometheus)
        self.assertIn("__PREPRODUCTION_METRICS_HOST__", prometheus)
        self.assertIn("environment_name: 生产环境", prometheus)
        self.assertIn("environment_name: RN预发布", prometheus)
        self.assertIn(".Labels.environment_name", alertmanager)

    def test_elk_is_internal_and_resource_bounded(self):
        compose = (MONITORING / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("elasticsearch:9.5.2", compose)
        self.assertIn("logstash:9.5.2", compose)
        self.assertIn("kibana:9.5.2", compose)
        self.assertNotIn('"9200:9200"', compose)
        self.assertNotIn('"5601:5601"', compose)
        self.assertIn("mem_limit: 1280m", compose)
        self.assertIn('"www4399.sbs:10.77.0.1"', compose)
        self.assertIn("SERVER_BASEPATH: /elk", compose)

    def test_renderer_uses_python39_compatible_file_writes(self):
        renderer = (MONITORING / "render_config.py").read_text(encoding="utf-8")
        self.assertNotIn(".write_text(", renderer)
        self.assertIn('.open("w", encoding="utf-8", newline="\\n")', renderer)

    def test_runtime_files_are_readable_only_by_service_users(self):
        renderer = (MONITORING / "render_config.py").read_text(encoding="utf-8")
        self.assertIn("PROMETHEUS_UID = 65534", renderer)
        self.assertIn("GRAFANA_UID = 472", renderer)
        self.assertIn("os.chown(temporary, uid, gid)", renderer)
        self.assertIn("os.chmod(temporary, mode)", renderer)
        self.assertIn("mode=0o440", renderer)

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

    def test_nginx_worker_can_serve_the_allowlisted_access_log(self):
        script = (ROOT / "nginx" / "12-log-permissions.sh").read_text(encoding="utf-8")
        dockerfile = (ROOT / "nginx" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("chown nginx:nginx /var/log/nginx/access_log.log", script)
        self.assertIn("chmod 0644 /var/log/nginx/access_log.log", script)
        self.assertIn("12-log-permissions.sh", dockerfile)


if __name__ == "__main__":
    unittest.main()
