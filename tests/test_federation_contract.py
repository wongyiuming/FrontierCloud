"""Deployment boundaries for the optional V1 control plane."""
import importlib.util
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

ROOT = Path(__file__).resolve().parents[1]
MAIN_SHA = "a" * 40
OLD_SHA = "d" * 40
PREVIOUS_SHA = "e" * 40


def load_updater_module():
    """Load the updater source on the CI host without requiring docker-py there."""
    fake_docker = types.ModuleType("docker")
    fake_docker.DockerClient = lambda *args, **kwargs: None
    previous = sys.modules.get("docker")
    sys.modules["docker"] = fake_docker
    try:
        spec = importlib.util.spec_from_file_location(
            "frontiercloud_updater_regression", ROOT / "updater/server.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load updater/server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("docker", None)
        else:
            sys.modules["docker"] = previous


class FederationContractTests(unittest.TestCase):
    def test_business_configuration_has_no_test_transport_or_role_switch(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for marker in ("cloudflared", "trycloudflare", "NODE_ROLE=", "MASTER_URL=", "SLAVE_URL=", "TLS_VERIFY=false"):
            self.assertNotIn(marker, compose + example)

    def test_github_actions_is_ci_only(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        self.assertIn("  browser-ui:", workflow)
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

    def test_round_one_browser_ui_is_part_of_authoritative_dev_ci(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        browser = workflow.split("  browser-ui:", 1)[1].split("  test-cluster:", 1)[0]
        self.assertIn("github.ref == 'refs/heads/dev'", browser)
        self.assertIn("tests/browser_ui_regression.py", browser)
        self.assertIn("docker compose up -d --build", browser)
        self.assertIn("PLAYWRIGHT_CHROMIUM_EXECUTABLE", browser)
        self.assertFalse((ROOT / ".github/workflows/ui-regression.yml").exists())

    def test_ci_jobs_have_role_appropriate_hard_limits(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        promotion = workflow.split("  promote-main:", 1)[1].split("  verify-promotion-query:", 1)[0]
        verification = workflow.split("  verify-promotion-query:", 1)[1].split("  browser-ui:", 1)[0]
        browser = workflow.split("  browser-ui:", 1)[1].split("  test-cluster:", 1)[0]
        cluster = workflow.split("  test-cluster:", 1)[1].split("  test-compose:", 1)[0]
        compose = workflow.split("  test-compose:", 1)[1]
        self.assertIn("timeout-minutes: 1", promotion)
        self.assertIn("timeout-minutes: 1", verification)
        self.assertIn("timeout-minutes: 5", browser)
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

    def test_updater_upgrade_is_exact_main_and_rollback_stays_in_main_history(self):
        updater = load_updater_module()

        def fake_git(*args, **kwargs):
            if args[:2] == ("status", "--porcelain"):
                return ""
            if args and args[0] == "fetch":
                self.assertIn("refs/heads/main", args[-1])
                return ""
            if args[:2] == ("rev-parse", "refs/remotes/origin/main"):
                return MAIN_SHA
            if args and args[0] == "cat-file":
                return ""
            raise AssertionError(f"unexpected git call: {args}")

        with (
            patch.object(updater, "git", side_effect=fake_git),
            patch.object(updater.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as merge_base,
        ):
            updater.validate_target(MAIN_SHA, "upgrade")
            updater.validate_target(OLD_SHA, "rollback")
            with self.assertRaisesRegex(RuntimeError, "current origin/main head"):
                updater.validate_target(OLD_SHA, "upgrade")
        self.assertGreaterEqual(merge_base.call_count, 2)

        with (
            patch.object(updater, "git", side_effect=fake_git),
            patch.object(updater.subprocess, "run", return_value=SimpleNamespace(returncode=1)),
        ):
            with self.assertRaisesRegex(RuntimeError, "not part of origin/main history"):
                updater.validate_target(OLD_SHA, "rollback")

    def test_updater_validation_failure_keeps_maintenance_enabled(self):
        updater = load_updater_module()
        target = MAIN_SHA
        maintenance = MagicMock()
        writes = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(updater, "FORCE_OPEN_FLAG", Path(directory) / "force-open"),
                patch.object(updater, "read_status", return_value={"current_sha": OLD_SHA, "previous_sha": PREVIOUS_SHA}),
                patch.object(updater, "write_status", writes),
                patch.object(updater, "maintenance", maintenance),
                patch.object(updater, "validate_target", side_effect=RuntimeError("target rejected")),
            ):
                updater.perform(target, "upgrade", True)
        self.assertEqual(maintenance.call_args_list, [call(True, target)])
        self.assertEqual(writes.call_args_list[-1].kwargs["state"], "failed")
        self.assertIn("target rejected", writes.call_args_list[-1].kwargs["detail"])

    def test_cluster_distribution_failure_keeps_master_maintenance_enabled(self):
        updater = load_updater_module()
        maintenance = MagicMock()
        writes = MagicMock()
        web = MagicMock()
        web.exec_run.return_value = SimpleNamespace(
            exit_code=1,
            output=(b"coordinator stdout", b"follower failed"),
        )
        engine = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(updater, "FORCE_OPEN_FLAG", Path(directory) / "force-open"),
                patch.object(updater, "read_status", return_value={"current_sha": MAIN_SHA, "previous_sha": PREVIOUS_SHA}),
                patch.object(updater, "write_status", writes),
                patch.object(updater, "maintenance", maintenance),
                patch.object(updater, "validate_target"),
                patch.object(updater, "client", return_value=engine),
                patch.object(updater, "service_container", return_value=web),
            ):
                updater.perform(MAIN_SHA, "upgrade", True)
        self.assertEqual(maintenance.call_args_list, [call(True, MAIN_SHA)])
        self.assertEqual(writes.call_args_list[-1].kwargs["state"], "failed")
        self.assertIn("cluster convergence failed", writes.call_args_list[-1].kwargs["detail"])
        engine.close.assert_called_once()

    def test_successful_release_is_the_only_path_that_clears_maintenance(self):
        updater = load_updater_module()
        maintenance = MagicMock()
        writes = MagicMock()
        engine = MagicMock()
        web = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(updater, "FORCE_OPEN_FLAG", Path(directory) / "force-open"),
                patch.object(updater, "read_status", return_value={"current_sha": MAIN_SHA, "previous_sha": PREVIOUS_SHA}),
                patch.object(updater, "write_status", writes),
                patch.object(updater, "maintenance", maintenance),
                patch.object(updater, "validate_target"),
                patch.object(updater, "client", return_value=engine),
                patch.object(updater, "service_container", return_value=web),
            ):
                updater.perform(MAIN_SHA, "upgrade", False)
        self.assertEqual(maintenance.call_args_list, [call(True, MAIN_SHA), call(False)])
        final = writes.call_args_list[-1].kwargs
        self.assertEqual(final["state"], "success")
        self.assertEqual(final["current_sha"], MAIN_SHA)
        self.assertEqual(final["previous_sha"], PREVIOUS_SHA)
        engine.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
