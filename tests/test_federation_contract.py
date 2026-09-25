"""Deployment boundaries for the optional V1 control plane."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FederationContractTests(unittest.TestCase):
    def test_business_configuration_has_no_test_transport_or_role_switch(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for marker in ("cloudflared", "trycloudflare", "NODE_ROLE=", "MASTER_URL=", "SLAVE_URL=", "TLS_VERIFY=false"):
            self.assertNotIn(marker, compose + example)

    def test_github_actions_is_ci_only(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        self.assertIn("  test-cluster:", workflow)
        self.assertIn("  test-compose:", workflow)
        for marker in (
            "  prepare-rn:", "  deploy-rn:", "  prepare-evoxt:", "  deploy-evoxt:",
            "self-hosted", "RN_DEPLOY_PATH", "EVOXT_DEPLOY_PATH", "deployments: write",
        ):
            self.assertNotIn(marker, workflow)
        self.assertNotIn("cloudflared", workflow)
        self.assertIn("ACCEPTANCE_GATEWAY", (ROOT / "tests/federation_stack.py").read_text(encoding="utf-8"))
        self.assertNotIn("curl -k", workflow)
        self.assertNotIn("ignore_https_errors", (ROOT / "tests/federation_stack.py").read_text(encoding="utf-8"))
        self.assertIn("ignore-certificate-errors-spki-list", (ROOT / "tests/federation_stack.py").read_text(encoding="utf-8"))

    def test_ci_jobs_have_role_appropriate_hard_limits(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        limits = [int(value) for value in re.findall(r"^    timeout-minutes: (\d+)$", workflow, re.MULTILINE)]
        self.assertEqual(sorted(limits), [1, 3, 3])
        promotion = workflow.split("  promote-main:", 1)[1].split("  test-cluster:", 1)[0]
        cluster = workflow.split("  test-cluster:", 1)[1].split("  test-compose:", 1)[0]
        compose = workflow.split("  test-compose:", 1)[1]
        self.assertIn("timeout-minutes: 1", promotion)
        self.assertIn("timeout-minutes: 3", cluster)
        self.assertIn("timeout-minutes: 3", compose)

    def test_integrity_client_javascript_parses(self):
        for relative in (
            "static/js/admin-upload-integrity.js",
            "static/js/karaoke-audio-quality.js",
        ):
            subprocess.run(["node", "--check", str(ROOT / relative)], check=True)

    def test_relay_has_verified_tls_and_no_disk_buffering(self):
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        relay = nginx.split("location ~ \"^/_relay_media/", 1)[1].split("location = /_relay_failure", 1)[0]
        for marker in ("internal;", "proxy_ssl_verify on;", "proxy_ssl_server_name on;", "proxy_buffering off;", "proxy_max_temp_file_size 0;",
                       "proxy_next_upstream off;", "proxy_ignore_headers X-Accel-Redirect;", 'proxy_set_header Cookie "";'):
            self.assertIn(marker, relay)
        self.assertIn("~^/_relay_media/ /api/v1/media/stream", nginx)


if __name__ == "__main__":
    unittest.main()