"""Deployment boundaries for the optional V1 control plane."""
import importlib.util
import re
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

    def test_main_is_promotion_only_and_dev_is_full_ci_authority(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        self.assertIn('branches: ["dev", "main"]', workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertIn("merge_group:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("promote-main:", workflow)
        self.assertIn("github.event_name == 'push' && github.ref == 'refs/heads/main'", workflow)
        for job in ("verify-promotion-query", "browser-ui", "test-cluster", "test-compose"):
            section = workflow.split(f"  {job}:", 1)[1]
            next_job = re.search(r"\n  [a-zA-Z0-9_-]+:\n", section)
            if next_job:
                section = section[:next_job.start()]
            self.assertIn("refs/heads/dev", section, job)
            self.assertNotIn("refs/heads/main", section, job)

    def test_main_promotion_requires_exact_merged_dev_sha_and_identical_tree(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        release = (ROOT / "app/services/release_control.py").read_text(encoding="utf-8")
        for source in (workflow, release):
            self.assertIn("parents[1]", source)
            self.assertIn("head_sha", source)
        self.assertIn("sourceTree !== mainTree", workflow)
        self.assertIn("item?.head_sha === sourceSha", workflow)
        self.assertIn("item?.head_branch === 'dev'", workflow)
        self.assertIn("run.status !== 'completed' || run.conclusion !== 'success'", workflow)
        self.assertIn('RELEASE_BRANCH = "main"', release)
        self.assertIn('CI_BRANCH = "dev"', release)
        self.assertIn("source_tree != main_tree", release)
        self.assertIn('item.get("head_branch") == CI_BRANCH', release)
        self.assertIn('item.get("event") == "push"', release)
        self.assertIn("completed and succeeded", release)

    def test_ci_permissions_are_read_only_and_have_no_deployment_authority(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        permissions = workflow.split("permissions:", 1)[1].split("\non:", 1)[0]
        self.assertIn("contents: read", permissions)
        self.assertIn("actions: read", permissions)
        for forbidden in (
            "contents: write", "actions: write", "deployments: write", "packages: write",
            "id-token: write", "self-hosted", "ssh-action", "scp-action",
        ):
            self.assertNotIn(forbidden, workflow)

    def test_release_policy_fast_gate_is_part_of_authoritative_dev_ci(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/check_release_policy.py", workflow)
        self.assertIn("python3 scripts/validate_env_contract.py", workflow)
        self.assertIn("Run source configuration tests", workflow)
        self.assertIn("Run unit and runtime tests", workflow)
        self.assertIn("tests/test_deployment_contract.py", workflow)
        self.assertIn("tests/test_federation_contract.py", workflow)
        self.assertIn("find tests -maxdepth 1 -type f -name 'test_*.py'", workflow)

    def test_only_updater_holds_the_docker_socket(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        updater_block = compose.split("  updater:\n", 1)[1].split("\n  web:\n", 1)[0]
        web_block = compose.split("  web:\n", 1)[1].split("\n  redis:\n", 1)[0]
        nginx_block = compose.split("  nginx:\n", 1)[1].split("\n  stun:\n", 1)[0]
        self.assertEqual(compose.count("/var/run/docker.sock:/var/run/docker.sock"), 1)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", updater_block)
        self.assertNotIn("docker.sock", web_block)
        self.assertNotIn("docker.sock", nginx_block)
        self.assertIn("read_only: true", updater_block)
        self.assertIn("security_opt: [no-new-privileges:true]", updater_block)
        self.assertIn("cap_drop: [ALL]", updater_block)
        self.assertNotIn("privileged:", updater_block)
        self.assertNotIn("ports:", updater_block)

    def test_web_can_only_reach_updater_through_control_volume(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        updater_block = compose.split("  updater:\n", 1)[1].split("\n  web:\n", 1)[0]
        web_block = compose.split("  web:\n", 1)[1].split("\n  redis:\n", 1)[0]
        nginx_block = compose.split("  nginx:\n", 1)[1].split("\n  stun:\n", 1)[0]
        self.assertIn("updater_control:/run/frontiercloud-updater", updater_block)
        self.assertIn("updater_control:/run/frontiercloud-updater", web_block)
        self.assertNotIn("updater_control:/run/frontiercloud-updater", nginx_block)
        self.assertEqual(compose.count("updater_control:/run/frontiercloud-updater"), 2)
        release = (ROOT / "app/services/release_control.py").read_text(encoding="utf-8")
        self.assertIn('CONTROL_SOCKET = "/run/frontiercloud-updater/control.sock"', release)
        self.assertIn("socket.AF_UNIX", release)
        self.assertNotIn("import docker", release)
        self.assertNotIn("docker.DockerClient", release)
        self.assertNotIn("/var/run/docker.sock", release)

    def test_runtime_upgrade_uses_engine_api_not_compose_or_systemd(self):
        updater = (ROOT / "updater/server.py").read_text(encoding="utf-8")
        updater_dockerfile = (ROOT / "updater/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("docker.DockerClient", updater)
        self.assertIn("engine.images.build", updater)
        self.assertIn("engine.api.create_container", updater)
        self.assertIn("containers.get", updater)
        self.assertNotIn("docker compose", updater.lower())
        self.assertNotIn("docker-compose", updater.lower())
        self.assertNotIn("systemctl", updater.lower())
        self.assertNotIn("docker-cli", updater_dockerfile.lower())
        self.assertNotIn("docker-cli-compose", updater_dockerfile.lower())
        self.assertIn("docker==7.1.0", updater_dockerfile)

    def test_updater_never_replaces_or_reexecutes_itself(self):
        updater = (ROOT / "updater/server.py").read_text(encoding="utf-8")
        replacement_loop = re.search(
            r'for service in \("secrets-init", "media-init", "web", "nginx"\):',
            updater,
        )
        self.assertIsNotNone(replacement_loop)
        self.assertNotIn('"updater"', replacement_loop.group(0))
        self.assertNotIn("os.exec", updater)
        self.assertNotIn("execv", updater)
        self.assertNotIn('restart("updater"', updater)

    def test_upgrade_and_rollback_targets_are_bound_to_main_history(self):
        updater = (ROOT / "updater/server.py").read_text(encoding="utf-8")
        self.assertIn('RELEASE_BRANCH = "main"', updater)
        self.assertIn("upgrade target is not the current origin/main head", updater)
        self.assertIn('"merge-base", "--is-ancestor", target, remote_ref', updater)
        self.assertIn("target is not part of origin/main history", updater)
        self.assertNotIn("origin/dev", updater)

    def test_cluster_distribution_runs_from_new_healthy_web_and_exact_target(self):
        updater = (ROOT / "updater/server.py").read_text(encoding="utf-8")
        coordinator = (ROOT / "app/services/cluster_update_coordinator.py").read_text(encoding="utf-8")
        self.assertLess(updater.index("wait_healthy(web)"), updater.index("app.services.cluster_update_coordinator"))
        self.assertIn('["python", "-m", "app.services.cluster_update_coordinator", target, mode]', updater)
        self.assertIn('"target_sha": target, "mode": mode', coordinator)
        self.assertIn('status.get("current_sha") != target', coordinator)
        self.assertIn('status.get("state") != "success"', coordinator)
        self.assertIn("cluster version convergence timed out", coordinator)

    def test_only_authenticated_upstream_master_can_control_follower_release(self):
        api = (ROOT / "app/api/internal_cluster_update.py").read_text(encoding="utf-8")
        self.assertIn("relation = await authenticated(request)", api)
        self.assertIn('state.node.get("role") != "Follower"', api)
        self.assertIn('relation.get("direction") != "upstream"', api)
        self.assertIn("Only the paired Master can control Follower releases", api)
        self.assertIn('"hold_maintenance": False', api)

    def test_master_is_the_only_cluster_release_coordinator(self):
        coordinator = (ROOT / "app/services/cluster_update_coordinator.py").read_text(encoding="utf-8")
        release = (ROOT / "app/services/release_control.py").read_text(encoding="utf-8")
        self.assertIn('state.node.get("role") != "Master"', coordinator)
        self.assertIn("cluster release coordinator must run on Master", coordinator)
        self.assertIn('state.node.get("role") != "Master"', release)
        self.assertIn("Only Master can start a cluster release", release)
        self.assertIn('row["direction"] == "downstream"', coordinator)
        self.assertIn('row["state"] == "active"', coordinator)

    def test_public_maintenance_gate_keeps_only_control_paths_reachable(self):
        gate = (ROOT / "nginx/maintenance-gate.conf").read_text(encoding="utf-8")
        self.assertIn(".frontiercloud-maintenance", gate)
        self.assertIn(".frontiercloud-force-open", gate)
        self.assertIn("api/v1/media/admin", gate)
        self.assertIn("internal/v1/cluster-update", gate)
        self.assertIn("health", gate)
        self.assertIn("metrics", gate)
        self.assertIn("return 503", gate)

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
