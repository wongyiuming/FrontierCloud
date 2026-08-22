import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExporterConfigurationTests(unittest.TestCase):
    def test_self_hosted_stun_is_stun_only_and_resource_bounded(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^  stun:\n(.*?)(?=^  [a-zA-Z][a-zA-Z0-9_]*:\n|\Z)",
            compose,
        )

        self.assertIsNotNone(match)
        block = match.group(1)
        self.assertIn("coturn/coturn:4.17.2-r0-alpine", block)
        self.assertIn("--stun-only", block)
        self.assertIn("--no-auth", block)
        self.assertIn("${WEBRTC_STUN_PORT:?WEBRTC_STUN_PORT must be set}", block)
        self.assertIn("mem_limit: 32m", block)
        self.assertIn("cpus:", block)
        self.assertIn("WEBRTC_STUN_URLS=stun:${WEBRTC_STUN_HOST}:${WEBRTC_STUN_PORT}", env_example)

    def test_media_ui_entrypoint_assets_bypass_static_cache(self):
        nginx = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")

        self.assertIn("location = /static/js/player.js", nginx)
        self.assertIn("location = /static/css/player.css", nginx)
        self.assertIn("location = /static/js/network-observation.js", nginx)

    def test_exporters_are_internal_only_and_resource_bounded(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        for service in (
            "node_exporter",
            "cadvisor",
            "redis_exporter",
            "mysql_exporter",
            "nginx_exporter",
            "nginxlog_exporter",
            "nginxlog_limiter",
            "mysql_backup",
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

    def test_nginx_worker_can_read_metrics_credentials(self):
        entrypoint = (ROOT / "nginx" / "10-metrics-auth.sh").read_text(encoding="utf-8")
        self.assertIn("install -d -o root -g nginx -m 0750 /run/frontiercloud", entrypoint)
        self.assertIn("chown root:nginx /run/frontiercloud/metrics.htpasswd", entrypoint)
        self.assertIn("chmod 0640 /run/frontiercloud/metrics.htpasswd", entrypoint)
        self.assertNotIn("chmod 0600 /run/frontiercloud/metrics.htpasswd", entrypoint)

    def test_application_metrics_supply_a_verified_proxy_identity(self):
        nginx = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        location = re.search(
            r"(?ms)location = /internal/metrics/app \{(.*?)^        \}", nginx
        )
        self.assertIsNotNone(location)
        block = location.group(1)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr;", block)
        self.assertIn('proxy_set_header X-Forwarded-For "";', block)
        self.assertIn('proxy_set_header Forwarded "";', block)

    def test_cadvisor_supports_docker_containerd_snapshotters(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("ghcr.io/google/cadvisor:0.56.2", compose)
        self.assertIn("--disable_metrics=disk", compose)

    def test_nginx_log_metrics_include_status_and_latency(self):
        nginx = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        entrypoint = (ROOT / "nginx" / "10-metrics-auth.sh").read_text(encoding="utf-8")
        exporter = (ROOT / "monitoring" / "nginxlog-exporter.hcl").read_text(encoding="utf-8")
        self.assertIn("$status", nginx)
        self.assertIn("$request_time", nginx)
        self.assertIn("$upstream_response_time", exporter)
        self.assertIn("touch /var/log/nginx/access_log.log", entrypoint)

    def test_nginx_metric_log_is_size_bounded(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        limiter = (ROOT / "monitoring" / "nginx_log_limiter.sh").read_text(encoding="utf-8")
        self.assertIn("NGINX_METRIC_LOG_MAX_BYTES", compose)
        self.assertIn(': > "$log"', limiter)
        self.assertIn("sleep 60", limiter)


if __name__ == "__main__":
    unittest.main()
