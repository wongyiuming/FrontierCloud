import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class CiAuthorityContractTests(unittest.TestCase):
    def test_main_is_promotion_only_and_dev_is_full_ci_authority(self):
        workflow = read(".github/workflows/docker.yml")

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
        workflow = read(".github/workflows/docker.yml")
        release = read("app/services/release_control.py")

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
        self.assertIn("item.get(\"head_sha\")", release)
        self.assertIn("item.get(\"head_branch\") == CI_BRANCH", release)
        self.assertIn('item.get("event") == "push"', release)
        self.assertIn("completed and succeeded", release)

    def test_ci_permissions_are_read_only_and_have_no_deployment_authority(self):
        workflow = read(".github/workflows/docker.yml")
        permissions = workflow.split("permissions:", 1)[1].split("\non:", 1)[0]

        self.assertIn("contents: read", permissions)
        self.assertIn("actions: read", permissions)
        for forbidden in (
            "contents: write",
            "actions: write",
            "deployments: write",
            "packages: write",
            "id-token: write",
            "self-hosted",
            "ssh-action",
            "scp-action",
        ):
            self.assertNotIn(forbidden, workflow)

    def test_release_policy_fast_gate_is_part_of_authoritative_dev_ci(self):
        workflow = read(".github/workflows/docker.yml")
        self.assertIn("python3 scripts/check_release_policy.py", workflow)
        self.assertIn("python3 scripts/validate_env_contract.py", workflow)
        self.assertIn("Run source configuration tests", workflow)
        self.assertIn("Run unit and runtime tests", workflow)
        self.assertIn("tests/test_deployment_contract.py tests/test_federation_contract.py", workflow)
        self.assertIn("find tests -maxdepth 1 -type f -name 'test_*.py'", workflow)


class RuntimeUpgradeBoundaryTests(unittest.TestCase):
    def test_only_updater_holds_the_docker_socket(self):
        compose = read("docker-compose.yaml")
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
        compose = read("docker-compose.yaml")
        updater_block = compose.split("  updater:\n", 1)[1].split("\n  web:\n", 1)[0]
        web_block = compose.split("  web:\n", 1)[1].split("\n  redis:\n", 1)[0]
        nginx_block = compose.split("  nginx:\n", 1)[1].split("\n  stun:\n", 1)[0]

        self.assertIn("updater_control:/run/frontiercloud-updater", updater_block)
        self.assertIn("updater_control:/run/frontiercloud-updater", web_block)
        self.assertNotIn("updater_control:/run/frontiercloud-updater", nginx_block)
        self.assertEqual(compose.count("updater_control:/run/frontiercloud-updater"), 2)

        release = read("app/services/release_control.py")
        self.assertIn('CONTROL_SOCKET = "/run/frontiercloud-updater/control.sock"', release)
        self.assertIn("socket.AF_UNIX", release)
        self.assertNotIn("docker", release.lower())

    def test_runtime_upgrade_uses_engine_api_not_compose_or_systemd(self):
        updater = read("updater/server.py")
        updater_dockerfile = read("updater/Dockerfile")

        self.assertIn("docker.DockerClient", updater)
        self.assertIn("engine.images.build", updater)
        self.assertIn("engine.api.create_container", updater)
        self.assertIn("containers.get", updater)
        self.assertNotIn("docker compose", updater.lower())
        self.assertNotIn("docker-compose", updater.lower())
        self.assertNotIn("systemctl", updater.lower())
        self.assertNotIn("subprocess.run([\"systemd", updater.lower())
        self.assertNotIn("docker-cli", updater_dockerfile.lower())
        self.assertNotIn("docker-cli-compose", updater_dockerfile.lower())
        self.assertIn("docker==7.1.0", updater_dockerfile)

    def test_updater_never_replaces_or_reexecutes_itself(self):
        updater = read("updater/server.py")
        replacement_loop = re.search(
            r'for service in \("secrets-init", "media-init", "web", "nginx"\):',
            updater,
        )
        self.assertIsNotNone(replacement_loop)
        self.assertNotIn('"updater"', replacement_loop.group(0))
        self.assertNotIn("os.exec", updater)
        self.assertNotIn("execv", updater)
        self.assertNotIn("restart(\"updater\"", updater)

    def test_upgrade_and_rollback_targets_are_bound_to_main_history(self):
        updater = read("updater/server.py")
        self.assertIn('RELEASE_BRANCH = "main"', updater)
        self.assertIn("upgrade target is not the current origin/main head", updater)
        self.assertIn('"merge-base", "--is-ancestor", target, remote_ref', updater)
        self.assertIn("target is not part of origin/main history", updater)
        self.assertNotIn("origin/dev", updater)

    def test_cluster_distribution_runs_from_new_healthy_web_and_exact_target(self):
        updater = read("updater/server.py")
        coordinator = read("app/services/cluster_update_coordinator.py")

        self.assertLess(updater.index("wait_healthy(web)"), updater.index("app.services.cluster_update_coordinator"))
        self.assertIn('["python", "-m", "app.services.cluster_update_coordinator", target, mode]', updater)
        self.assertIn('"target_sha": target, "mode": mode', coordinator)
        self.assertIn('status.get("current_sha") != target', coordinator)
        self.assertIn('status.get("state") != "success"', coordinator)
        self.assertIn("cluster version convergence timed out", coordinator)


class FederationControlBoundaryTests(unittest.TestCase):
    def test_only_authenticated_upstream_master_can_control_follower_release(self):
        api = read("app/api/internal_cluster_update.py")
        self.assertIn("relation = await authenticated(request)", api)
        self.assertIn('state.node.get("role") != "Follower"', api)
        self.assertIn('relation.get("direction") != "upstream"', api)
        self.assertIn("Only the paired Master can control Follower releases", api)
        self.assertIn('"hold_maintenance": False', api)

    def test_master_is_the_only_cluster_release_coordinator(self):
        coordinator = read("app/services/cluster_update_coordinator.py")
        release = read("app/services/release_control.py")

        self.assertIn('state.node.get("role") != "Master"', coordinator)
        self.assertIn("cluster release coordinator must run on Master", coordinator)
        self.assertIn('state.node.get("role") != "Master"', release)
        self.assertIn("Only Master can start a cluster release", release)
        self.assertIn('row["direction"] == "downstream"', coordinator)
        self.assertIn('row["state"] == "active"', coordinator)

    def test_public_maintenance_gate_keeps_only_control_paths_reachable(self):
        gate = read("nginx/maintenance-gate.conf")
        self.assertIn(".frontiercloud-maintenance", gate)
        self.assertIn(".frontiercloud-force-open", gate)
        self.assertIn("api/v1/media/admin", gate)
        self.assertIn("internal/v1/cluster-update", gate)
        self.assertIn("health", gate)
        self.assertIn("metrics", gate)
        self.assertIn("return 503", gate)


if __name__ == "__main__":
    unittest.main()
